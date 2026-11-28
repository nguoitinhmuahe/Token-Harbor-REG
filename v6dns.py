from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request

TTL = 600.0
DOH = "https://cloudflare-dns.com/dns-query?name=%s&type=AAAA"

_cache: dict = {}
_lock = threading.Lock()


def resolve_v6(host: str) -> list:
    now = time.time()
    with _lock:
        c = _cache.get(host)
        if c and c[0] > now:
            return list(c[1])
    ips = []
    try:
        ai = socket.getaddrinfo(host, 443, socket.AF_INET6, socket.SOCK_STREAM)
        ips = [a[4][0] for a in ai]
    except OSError:
        pass
    if not ips:
        try:
            req = urllib.request.Request(DOH % host,
                                         headers={"accept": "application/dns-json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            ips = [a["data"] for a in d.get("Answer", [])
                   if a.get("type") == 28 and a.get("data")]
        except Exception:
            ips = []
    if ips:
        with _lock:
            _cache[host] = (now + TTL, tuple(ips))
    return ips


def resolve_v4(host: str) -> list:
    now = time.time()
    key = "4:" + host
    with _lock:
        c = _cache.get(key)
        if c and c[0] > now:
            return list(c[1])
    ips = []
    try:
        ai = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        ips = [a[4][0] for a in ai]
    except OSError:
        pass
    if not ips:
        try:
            req = urllib.request.Request(
                "https://cloudflare-dns.com/dns-query?name=%s&type=A" % host,
                headers={"accept": "application/dns-json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            ips = [a["data"] for a in d.get("Answer", [])
                   if a.get("type") == 1 and a.get("data")]
        except Exception:
            ips = []
    if ips:
        with _lock:
            _cache[key] = (now + TTL, tuple(ips))
    return ips
