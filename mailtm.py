
from __future__ import annotations

import asyncio
import random
import string
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import fastfox

BASE = "https://api.mail.tm"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    retry = Retry(total=4, backoff_factor=1.5,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "POST"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def _rand(n=10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def domains() -> list[str]:
    r = _session().get(f"{BASE}/domains", timeout=20)
    r.raise_for_status()
    data = r.json()
    items = data if isinstance(data, list) else data.get("hydra:member", [])
    return [d["domain"] if isinstance(d, dict) else str(d) for d in items]


def create(address: str, password: str) -> dict:
    r = _session().post(f"{BASE}/accounts",
                        json={"address": address, "password": password}, timeout=25)
    if r.status_code not in (201, 200, 422):
        r.raise_for_status()
    return r.json() if r.content else {}


def token(address: str, password: str) -> str:
    r = _session().post(f"{BASE}/token",
                        json={"address": address, "password": password}, timeout=25)
    r.raise_for_status()
    return r.json()["token"]


def messages(tok: str) -> list[dict]:
    r = _session().get(f"{BASE}/messages",
                       headers={"Authorization": f"Bearer {tok}"}, timeout=25)
    if r.status_code == 429:
        raise RuntimeError("mailtm_429")
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("hydra:member", [])


def message(tok: str, mid: str) -> dict:
    r = _session().get(f"{BASE}/messages/{mid}",
                       headers={"Authorization": f"Bearer {tok}"}, timeout=25)
    if r.status_code == 429:
        return {}
    r.raise_for_status()
    return r.json()


def _body_text(msg: dict) -> str:
    parts = []
    for key in ("text", "html"):
        v = msg.get(key)
        if isinstance(v, list):
            parts.append("\n".join(str(x) for x in v))
        elif isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)


def _find_link(tok: str) -> str | None:
    for m in messages(tok):
        sender = (m.get("from") or {}).get("address", "").lower()
        subject = (m.get("subject") or "").lower()
        if "tokenharbor" not in sender and "verify" not in subject:
            continue
        full = message(tok, m["id"])
        link = fastfox.extract_verify_link(_body_text(full))
        if link:
            return link
    return None


def find_once(tok: str) -> str | None:
    return _find_link(tok)


async def wait_verify(tok: str, *, attempts: int = 90, interval: float = 3.0) -> str | None:
    delay = interval
    for _ in range(attempts):
        try:
            link = await asyncio.to_thread(_find_link, tok)
            if link:
                return link
            delay = interval
        except Exception:
            delay = min(delay * 1.6, 12.0)
        await asyncio.sleep(delay)
    return None
