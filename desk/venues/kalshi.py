"""Kalshi Trade API v2 client (event contracts).

Prices are YES-price dollars everywhere in this desk. book side: bid = long YES, ask = long NO.
Public market data works without a key. Orders need a key (read + write::trade scopes is enough;
the desk never calls transfer endpoints).
"""
from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..util import fnum

API_PREFIX = "/trade-api/v2"


class KalshiError(Exception):
    def __init__(self, status: int, body: Any, path: str):
        super().__init__(f"{status} {path}: {str(body)[:300]}")
        self.status, self.body, self.path = status, body, path


class MalformedRequest(KalshiError):
    """400/422 - never retried; the request is wrong and will stay wrong."""


class RateLimited(KalshiError):
    pass


class Unavailable(KalshiError):
    pass


@dataclass
class Book:
    ticker: str
    ts: float
    bids: list[tuple[float, float]]          # yes bids, best first: (price, size)
    asks: list[tuple[float, float]]          # yes asks (from no bids), best first

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


def normalize_pem(text: str) -> str:
    """Accept a key pasted with or without its BEGIN/END lines (phones often copy only the body)."""
    t = text.strip()
    if "-----BEGIN" in t:
        return t + "\n"
    body = "".join(t.split())
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN PRIVATE KEY-----\n{lines}\n-----END PRIVATE KEY-----\n"


@dataclass
class Signer:
    key_id: str
    pem: str
    _key: Any = field(default=None, repr=False)

    def __post_init__(self):
        self.pem = normalize_pem(self.pem)
        self._key = serialization.load_pem_private_key(self.pem.encode(), password=None)

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method.upper()}{path.split('?')[0]}".encode()
        if isinstance(self._key, Ed25519PrivateKey):
            sig = self._key.sign(msg)
        else:
            sig = self._key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                  salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


def parse_book(ticker: str, data: dict) -> Book:
    ob = data.get("orderbook_fp") or {}
    yes = [(fnum(p, 0.0), fnum(s, 0.0)) for p, s in (ob.get("yes_dollars") or [])]
    no = [(fnum(p, 0.0), fnum(s, 0.0)) for p, s in (ob.get("no_dollars") or [])]
    bids = sorted([x for x in yes if x[1] > 0], key=lambda x: -x[0])
    asks = sorted([(round(1.0 - p, 4), s) for p, s in no if s > 0], key=lambda x: x[0])
    return Book(ticker=ticker, ts=time.time(), bids=bids, asks=asks)


class KalshiClient:
    def __init__(self, base_url: str, signer: Signer | None = None, alt_base_url: str | None = None,
                 timeout: float = 10.0, max_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.alt_base_url = alt_base_url.rstrip("/") if alt_base_url else None
        self.signer = signer
        self.max_retries = max_retries
        self._http = httpx.AsyncClient(timeout=timeout, http2=False,
                                       headers={"User-Agent": "trading-desk/1.0"})
        self._consecutive_429 = 0

    @property
    def authed(self) -> bool:
        return self.signer is not None

    async def close(self):
        await self._http.aclose()

    async def _req(self, method: str, path: str, params: dict | None = None, json: dict | None = None,
                   auth: bool = False) -> dict:
        full_path = API_PREFIX + path
        delay = 0.5
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            base = self.base_url
            if self.alt_base_url and self._consecutive_429 >= 3:
                base = self.alt_base_url
            headers = {}
            if self.signer:
                headers.update(self.signer.headers(method, full_path))
            elif auth:
                raise KalshiError(401, "no API key configured", path)
            try:
                resp = await self._http.request(method, base.replace(API_PREFIX, "") + full_path,
                                                params=params, json=json, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = Unavailable(0, repr(e), path)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            if resp.status_code == 429:
                self._consecutive_429 += 1
                last = RateLimited(429, resp.text, path)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            self._consecutive_429 = 0
            if resp.status_code in (400, 404, 409, 422):
                raise MalformedRequest(resp.status_code, _safe_json(resp), path)
            if resp.status_code in (401, 403):
                raise KalshiError(resp.status_code, _safe_json(resp), path)
            if resp.status_code >= 500:
                last = Unavailable(resp.status_code, resp.text, path)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            return _safe_json(resp)
        assert last is not None
        raise last

    # ---- public market data ----
    async def exchange_status(self) -> dict:
        return await self._req("GET", "/exchange/status")

    async def markets(self, series_ticker: str | None = None, status: str = "open", limit: int = 500,
                      event_ticker: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"status": status, "limit": limit, "mve_filter": "exclude"}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        out: list[dict] = []
        cursor = None
        for _ in range(5):
            if cursor:
                params["cursor"] = cursor
            data = await self._req("GET", "/markets", params=params)
            out.extend(data.get("markets") or [])
            cursor = data.get("cursor")
            if not cursor or len(data.get("markets") or []) < limit:
                break
        return out

    async def market(self, ticker: str) -> dict:
        return (await self._req("GET", f"/markets/{ticker}")).get("market", {})

    async def series(self, ticker: str) -> dict:
        return (await self._req("GET", f"/series/{ticker}")).get("series", {})

    async def orderbook(self, ticker: str, depth: int = 10) -> Book:
        data = await self._req("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return parse_book(ticker, data)

    async def candles(self, series: str, ticker: str, start_ts: int, end_ts: int, period: int = 1) -> list[dict]:
        data = await self._req("GET", f"/series/{series}/markets/{ticker}/candlesticks",
                               params={"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": period})
        return data.get("candlesticks") or []

    async def trades(self, ticker: str, min_ts: int | None = None, limit: int = 500) -> list[dict]:
        params: dict[str, Any] = {"ticker": ticker, "limit": limit}
        if min_ts:
            params["min_ts"] = int(min_ts)
        data = await self._req("GET", "/markets/trades", params=params)
        return data.get("trades") or []

    # ---- authenticated ----
    async def balance(self) -> dict:
        return await self._req("GET", "/portfolio/balance", auth=True)

    async def positions(self) -> list[dict]:
        data = await self._req("GET", "/portfolio/positions", params={"limit": 200}, auth=True)
        return data.get("market_positions") or []

    async def resting_orders(self, ticker: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"status": "resting", "limit": 200}
        if ticker:
            params["ticker"] = ticker
        data = await self._req("GET", "/portfolio/orders", params=params, auth=True)
        return data.get("orders") or []

    async def order(self, order_id: str) -> dict:
        return (await self._req("GET", f"/portfolio/orders/{order_id}", auth=True)).get("order", {})

    async def fills(self, min_ts: int | None = None, ticker: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": 200}
        if min_ts:
            params["min_ts"] = int(min_ts)
        if ticker:
            params["ticker"] = ticker
        data = await self._req("GET", "/portfolio/fills", params=params, auth=True)
        return data.get("fills") or []

    async def create_order(self, ticker: str, side: str, count: float, price: float, client_order_id: str,
                           post_only: bool = False, tif: str = "good_till_canceled",
                           reduce_only: bool = False, expiration_ts: int | None = None) -> dict:
        assert side in ("bid", "ask")
        body: dict[str, Any] = {
            "ticker": ticker, "client_order_id": client_order_id, "side": side,
            "count": f"{count:.2f}", "price": f"{price:.4f}", "time_in_force": tif,
            "self_trade_prevention_type": "maker", "post_only": post_only,
            "cancel_order_on_pause": True, "reduce_only": reduce_only,
        }
        if expiration_ts:
            body["expiration_time"] = int(expiration_ts)
        return await self._req("POST", "/portfolio/events/orders", json=body, auth=True)

    async def cancel_order(self, order_id: str, ticker: str) -> dict:
        return await self._req("DELETE", f"/portfolio/events/orders/{order_id}",
                               params={"market_ticker": ticker}, auth=True)

    async def cancel_all(self) -> dict:
        return await self._req("DELETE", "/portfolio/events/orders", auth=True)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:500]}
