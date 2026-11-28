
from __future__ import annotations

import random
import string
import time

import requests

import fastfox
import mailtm

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0"
TIMEOUT = 25

PROVIDERS = ("mailtm", "duckmail", "guerrilla", "tempmailio", "inboxes")
RANDOM_POOL = PROVIDERS

LABELS = {
    "random": "random (different service per attempt)",
    "mailtm": "mail.tm",
    "duckmail": "duckmail.sbs",
    "guerrilla": "guerrilla mail",
    "tempmailio": "temp-mail.io",
    "inboxes": "inboxes.com",
}

_HYDRA = {"mailtm": "https://api.mail.tm", "duckmail": "https://api.duckmail.sbs"}
_GUERRILLA = "https://api.guerrillamail.com/ajax.php"
_TMIO = "https://api.internal.temp-mail.io/api/v3"
_INBOXES = "https://inboxes.com/api/v2"


class Mailbox:

    __slots__ = ("provider", "email", "token")

    def __init__(self, provider: str, email: str, token):
        self.provider = provider
        self.email = email
        self.token = token

    def __repr__(self) -> str:
        return f"Mailbox({self.provider}, {self.email!r})"


def _rand(n: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _req(method: str, url: str, **kw) -> requests.Response:
    h = {"User-Agent": UA}
    h.update(kw.pop("headers", {}) or {})
    kw.setdefault("timeout", TIMEOUT)
    last = None
    for i in range(3):
        try:
            r = requests.request(method, url, headers=h, **kw)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            time.sleep(0.6 * (i + 1))
            continue
        if r.status_code == 429:
            raise RuntimeError("mail_429")
        r.raise_for_status()
        return r
    raise last


def _get(url: str, **kw) -> requests.Response:
    return _req("GET", url, **kw)


def _post(url: str, **kw) -> requests.Response:
    return _req("POST", url, **kw)


def _msg_body(msg: dict) -> str:
    parts = []
    for key in ("text", "html", "body_text", "body_html", "mail_body", "body",
                "mail_excerpt", "ph"):
        v = msg.get(key)
        if isinstance(v, list):
            parts.append("\n".join(str(x) for x in v))
        elif isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)


def _hydra_new(base: str, name: str, password: str) -> Mailbox:
    for attempt in range(2):
        data = _get(f"{base}/domains").json()
        items = data if isinstance(data, list) else data.get("hydra:member", [])
        doms = [d["domain"] for d in items
                if isinstance(d, dict) and d.get("isActive", True) and d.get("domain")]
        if not doms:
            raise RuntimeError("no domains")
        address = f"{name}@{random.choice(doms)}"
        r = requests.post(f"{base}/accounts",
                          json={"address": address, "password": password},
                          headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code not in (200, 201, 422):
            r.raise_for_status()
        try:
            tok = _post(f"{base}/token",
                        json={"address": address, "password": password}).json()["token"]
        except (requests.HTTPError, KeyError):
            continue
        return Mailbox("mailtm" if base.endswith("mail.tm") else "duckmail",
                       address, tok)
    raise RuntimeError("mail_token_failed")


def _hydra_messages(base: str, tok: str) -> list[dict]:
    data = _get(f"{base}/messages", headers={"Authorization": f"Bearer {tok}"}).json()
    return data if isinstance(data, list) else data.get("hydra:member", [])


def _hydra_message(base: str, tok: str, mid: str) -> dict:
    return _get(f"{base}/messages/{mid}", headers={"Authorization": f"Bearer {tok}"}).json()


def _new_guerrilla(name: str, password: str) -> Mailbox:
    j = _get(_GUERRILLA, params={"f": "get_email_address", "lang": "en"}).json()
    return Mailbox("guerrilla", j["email_addr"], j["sid_token"])


def _new_tmio(name: str, password: str) -> Mailbox:
    j = _post(f"{_TMIO}/email/new", json={"min_name_length": 8}).json()
    return Mailbox("tempmailio", j["email"], j["token"])


def _new_inboxes(name: str, password: str) -> Mailbox:
    j = _post(f"{_INBOXES}/inbox", json={}).json()
    return Mailbox("inboxes", j["inbox"], None)


def new(provider: str, name: str | None = None, password: str = "") -> Mailbox:
    provider = (provider or "mailtm").strip().lower()
    name = name or _rand()
    if provider == "random":
        provider = random.choice(RANDOM_POOL)

    if provider in ("mailtm", "mail.tm"):
        return _hydra_new(_HYDRA["mailtm"], name, password)
    if provider in ("duckmail", "duckmail.sbs", "duck"):
        return _hydra_new(_HYDRA["duckmail"], name, password)
    if provider in ("guerrilla", "guerrillamail"):
        return _new_guerrilla(name, password)
    if provider in ("tempmailio", "temp-mail.io", "temp-mail"):
        return _new_tmio(name, password)
    if provider == "inboxes":
        return _new_inboxes(name, password)
    raise ValueError(f"unknown mail provider: {provider}")


def chain(provider: str) -> list[str]:
    provider = (provider or "mailtm").strip().lower()
    if provider == "random":
        first = random.choice(RANDOM_POOL)
    elif provider in RANDOM_POOL:
        first = provider
    else:
        first = "mailtm"
    rest = [p for p in RANDOM_POOL if p != first]
    random.shuffle(rest)
    return [first] + rest


def _list(mb: Mailbox) -> list[dict]:
    p, tok = mb.provider, mb.token
    if p == "mailtm":
        return _hydra_messages(_HYDRA["mailtm"], tok)
    if p == "duckmail":
        return _hydra_messages(_HYDRA["duckmail"], tok)
    if p == "guerrilla":
        j = _get(_GUERRILLA, params={"f": "check_email", "seq": 0, "sid_token": tok}).json()
        return j.get("list") or []
    if p == "tempmailio":
        try:
            j = _get(f"{_TMIO}/email/{mb.email}/messages", params={"token": tok}).json()
        except requests.HTTPError:
            j = _get(f"{_TMIO}/email/{mb.email}/messages").json()
        return j if isinstance(j, list) else []
    if p == "inboxes":
        j = _get(f"{_INBOXES}/inbox/{mb.email}").json()
        return j.get("msgs") or []
    return []


def _detail(mb: Mailbox, m: dict) -> str:
    p, tok = mb.provider, mb.token
    mid = m.get("id") or m.get("mail_id") or m.get("_id") or m.get("uid")
    if not mid:
        return ""
    try:
        if p in _HYDRA:
            return _msg_body(_hydra_message(_HYDRA[p], tok, str(mid)))
        if p == "guerrilla":
            j = _get(_GUERRILLA, params={"f": "fetch_email", "email_id": mid,
                                         "sid_token": tok}).json()
            return _msg_body(j)
        if p == "tempmailio":
            r = requests.get(f"{_TMIO}/email/{mb.email}/messages/{mid}",
                             params={"token": tok}, headers={"User-Agent": UA},
                             timeout=TIMEOUT)
            if r.status_code != 200:
                r = requests.get(f"{_TMIO}/email/{mb.email}/messages/{mid}",
                                 headers={"User-Agent": UA}, timeout=TIMEOUT)
            return _msg_body(r.json()) if r.status_code == 200 else ""
        if p == "inboxes":
            r = requests.get(f"https://inboxes.com/read/{mid}",
                             headers={"User-Agent": UA}, timeout=TIMEOUT)
            return r.text if r.status_code == 200 else ""
    except Exception:
        return ""
    return ""


def find_once(mb) -> str | None:
    if not isinstance(mb, Mailbox):
        return mailtm.find_once(str(mb))

    for m in _list(mb):
        sender = str(m.get("from") or m.get("mail_from") or m.get("f") or "").lower()
        subject = str(m.get("subject") or m.get("mail_subject") or m.get("s") or "").lower()
        relevant = ("harbor" in sender or "harbor" in subject
                    or "verify" in subject or "token" in subject)
        body = _msg_body(m)
        if not body and not relevant and (sender or subject):
            continue
        body += "\n" + _detail(mb, m)
        link = fastfox.extract_verify_link(body)
        if link:
            return link
    return None


if __name__ == "__main__":
    for p in PROVIDERS:
        try:
            mb = new(p, "selftest" + _rand(6), "Pw" + _rand(12))
            got = find_once(mb)
            assert mb.email and "@" in mb.email, mb.email
            print(f"OK   {p:11} {mb.email}  link={bool(got)}")
        except Exception as e:
            print(f"FAIL {p:11} {type(e).__name__}: {e}")
