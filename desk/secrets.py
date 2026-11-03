"""Secret files. Never logged, never in code, never in the repo.

Layout under DESK_SECRETS:
  desk_secret                    shared engine<->judge auth (group desk)
  kalshi_key_id, kalshi_key.pem  engine only
  kalshi_demo_key_id, kalshi_demo_key.pem  engine only
  anthropic_api_key              judge/seats only
  dashboard_password             engine only (pbkdf2 hash)
  inbox/                         engine drops the Anthropic key here at setup; judge moves it
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets as _secrets
from pathlib import Path

from .config import secrets_dir


def _p(name: str) -> Path:
    return secrets_dir() / name


def read(name: str) -> str | None:
    p = _p(name)
    try:
        v = p.read_text().strip()
        return v or None
    except (FileNotFoundError, PermissionError):
        return None


def write(name: str, value: str, mode: int = 0o600) -> None:
    p = _p(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(value.strip() + "\n")
    os.chmod(tmp, mode)
    tmp.replace(p)


def exists(name: str) -> bool:
    return _p(name).exists()


def desk_secret() -> str:
    v = os.environ.get("DESK_SECRET") or read("desk_secret")
    if not v:
        v = _secrets.token_urlsafe(32)
        write("desk_secret", v, 0o640)
    return v


def inbox_dir() -> Path:
    d = secrets_dir() / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def drop_in_inbox(name: str, value: str) -> None:
    p = inbox_dir() / name
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as f:
        f.write(value.strip() + "\n")


def collect_inbox(name: str) -> bool:
    """Judge side: move a dropped secret into its own 0600 file."""
    p = inbox_dir() / name
    if not p.exists():
        return False
    try:
        val = p.read_text().strip()
    except PermissionError:
        return False
    if val:
        write(name, val, 0o600)
    try:
        p.unlink()
    except OSError:
        pass
    return bool(val)


def hash_password(pw: str) -> str:
    salt = _secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_password(pw: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    s, h = stored.split("$", 1)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(s), 200_000)
    return hmac.compare_digest(base64.b64encode(dk).decode(), h)
