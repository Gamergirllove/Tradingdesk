"""Phone push via ntfy (free app, no account). Topic lives in <data>/ntfy_topic."""
from __future__ import annotations

import os

import httpx

from .config import data_dir


def topic() -> str | None:
    t = os.environ.get("NTFY_TOPIC")
    if t:
        return t.strip()
    p = data_dir() / "ntfy_topic"
    return p.read_text().strip() if p.exists() else None


async def push(title: str, text: str, priority: str = "default", server: str = "https://ntfy.sh",
               click: str | None = None, tags: str | None = None) -> bool:
    t = topic()
    if not t:
        return False
    headers = {"Title": title[:120], "Priority": priority}
    if click:
        headers["Click"] = click
    if tags:
        headers["Tags"] = tags
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{server.rstrip('/')}/{t}", content=text[:3500].encode(), headers=headers)
            return r.status_code < 300
    except httpx.HTTPError:
        return False
