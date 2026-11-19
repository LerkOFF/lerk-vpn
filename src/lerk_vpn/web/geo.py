"""Geo-базы (geoip.dat, geosite.dat): загрузка, список тегов, проверка правил и адресов."""

from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

DEFAULT_URLS = {
    "geoip": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat",
    "geosite": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat",
}
# российские сервисы, чьи домены не только в зоне .ru (берутся те, что есть в базе)
RU_SERVICE_TAGS = [
    "category-ru", "yandex", "vk", "mailru", "mailru-group", "sber", "sberbank", "tbank-ru", "ozon", "wildberries",
    "rutube", "category-gov-ru", "category-bank-ru", "category-media-ru", "category-ecommerce-ru", "category-retail-ru",
]


def _varint(b: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return n, i


def index_file(path: Path) -> dict[str, list[int]]:
    """{ТЕГ: [смещение сообщения, длина, число записей]} по protobuf-файлу GeoIPList или GeoSiteList."""
    b = path.read_bytes()
    i, out = 0, {}
    while i < len(b):
        _, i = _varint(b, i)
        ln, i = _varint(b, i)
        start, end = i, i + ln
        j, code, count = start, "", 0
        while j < end:
            t, j = _varint(b, j)
            wt, fld = t & 7, t >> 3
            if wt == 2:
                l, j = _varint(b, j)
                if fld == 1 and not code:
                    code = b[j:j + l].decode("utf-8", "replace")
                else:
                    count += 1
                j += l
            elif wt == 0:
                _, j = _varint(b, j)
            elif wt == 5:
                j += 4
            elif wt == 1:
                j += 8
            else:
                break
        out[code.lower()] = [start, ln, count]
        i = end
    return out


class Geo:
    def __init__(self, folder: Path):
        self.dir = folder
        self._idx: dict[str, dict] = {}
        self._mtime: dict[str, float] = {}
        self._lock = threading.Lock()

    def path(self, kind: str) -> Path:
        return self.dir / f"{kind}.dat"

    def _index(self, kind: str) -> dict:
        p = self.path(kind)
        if not p.exists():
            return {}
        m = p.stat().st_mtime
        with self._lock:
            if self._mtime.get(kind) != m:
                cache = self.dir / f"{kind}.index.json"
                data = None
                if cache.exists() and cache.stat().st_mtime >= m:
                    try:
                        data = json.loads(cache.read_text("utf-8"))
                    except (OSError, ValueError):
                        data = None
                if data is None:
                    data = index_file(p)
                    tmp = cache.with_suffix(".tmp")
                    tmp.write_text(json.dumps(data), "utf-8")
                    tmp.replace(cache)
                self._idx[kind], self._mtime[kind] = data, m
            return self._idx[kind]

    # --- состояние ---
    def loaded(self) -> bool:
        return self.path("geoip").exists() and self.path("geosite").exists()

    def status(self) -> dict:
        out = {}
        for kind in ("geoip", "geosite"):
            p = self.path(kind)
            out[kind] = {"exists": p.exists(), "size": p.stat().st_size if p.exists() else 0, "mtime": int(p.stat().st_mtime) if p.exists() else 0, "tags": len(self._index(kind)) if p.exists() else 0}
        return out

    def has_tag(self, kind: str, tag: str) -> bool:
        return tag.lower() in self._index(kind)

    def search(self, kind: str, q: str, limit: int = 60) -> list[tuple[str, int]]:
        q = q.lower().strip()
        rows = [(t, v[2]) for t, v in self._index(kind).items() if t and q in t]
        rows.sort(key=lambda r: (not r[0].startswith(q), len(r[0]), r[0]))
        return rows[:limit]

    # --- загрузка ---
    def download(self, urls: dict[str, str], log=print) -> bool:
        self.dir.mkdir(parents=True, exist_ok=True)
        ok = True
        for kind in ("geoip", "geosite"):
            url = urls.get(kind) or DEFAULT_URLS[kind]
            if not url.startswith("https://"):
                log(f"{kind}: адрес должен начинаться с https://")
                ok = False
                continue
            tmp = self.path(kind).with_suffix(".part")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "lerk-vpn"})
                with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as fh:
                    while chunk := r.read(1 << 20):
                        fh.write(chunk)
                index_file(tmp)  # проверяем, что файл разбирается
                os.replace(tmp, self.path(kind))
                self._mtime.pop(kind, None)
                log(f"{kind}: скачано {self.path(kind).stat().st_size // 1024} КБ, тегов {len(self._index(kind))}")
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                log(f"{kind}: не удалось скачать ({type(exc).__name__}: {exc})")
                ok = False
        return ok

    # --- содержимое тега ---
    def _entries(self, kind: str, tag: str) -> list[bytes]:
        meta = self._index(kind).get(tag.lower())
        if not meta:
            return []
        b = self.path(kind).read_bytes()
        start, ln = meta[0], meta[1]
        j, end, out = start, start + ln, []
        first = True
        while j < end:
            t, j = _varint(b, j)
            wt = t & 7
            if wt == 2:
                l, j = _varint(b, j)
                if not (first and t >> 3 == 1):
                    out.append(b[j:j + l])
                first = False if t >> 3 == 1 else first
                j += l
            elif wt == 0:
                _, j = _varint(b, j)
            else:
                break
        return out

    def domain_rules(self, tag: str) -> list[tuple[int, str]]:
        """(тип, значение): 0 подстрока, 1 regexp, 2 домен с поддоменами, 3 точный."""
        out = []
        for e in self._entries("geosite", tag):
            j, typ, val = 0, 0, ""
            while j < len(e):
                t, j = _varint(e, j)
                if t & 7 == 0:
                    v, j = _varint(e, j)
                    if t >> 3 == 1:
                        typ = v
                elif t & 7 == 2:
                    l, j = _varint(e, j)
                    if t >> 3 == 2:
                        val = e[j:j + l].decode("utf-8", "replace")
                    j += l
                else:
                    break
            out.append((typ, val))
        return out

    def cidrs(self, tag: str) -> list[ipaddress._BaseNetwork]:
        nets = []
        for e in self._entries("geoip", tag):
            j, raw, prefix = 0, b"", 0
            while j < len(e):
                t, j = _varint(e, j)
                if t & 7 == 2:
                    l, j = _varint(e, j)
                    if t >> 3 == 1:
                        raw = e[j:j + l]
                    j += l
                elif t & 7 == 0:
                    v, j = _varint(e, j)
                    if t >> 3 == 2:
                        prefix = v
                else:
                    break
            try:
                nets.append(ipaddress.ip_network((ipaddress.ip_address(raw), prefix), strict=False))
            except ValueError:
                continue
        return nets


def domain_matches(host: str, rule: str, geo: Geo | None) -> str | None:
    """Если правило подходит к домену, возвращает описание, иначе None."""
    host = host.lower().rstrip(".")
    if rule.startswith("geosite:"):
        if not geo:
            return None
        tag = rule[8:]
        for typ, val in geo.domain_rules(tag):
            v = val.lower()
            if (typ == 2 and (host == v or host.endswith("." + v))) or (typ == 3 and host == v) or (typ == 0 and v in host):
                return f"{rule} ({'domain:' if typ == 2 else 'full:' if typ == 3 else ''}{val})"
        return None
    if rule.startswith("domain:"):
        v = rule[7:].lower()
        return rule if host == v or host.endswith("." + v) else None
    if rule.startswith("full:"):
        return rule if host == rule[5:].lower() else None
    if rule.startswith("keyword:"):
        return rule if rule[8:].lower() in host else None
    v = rule.lower()
    return rule if host == v or host.endswith("." + v) else None


def ip_matches(ip: ipaddress._BaseAddress, rule: str, geo: Geo | None) -> str | None:
    if rule.startswith("geoip:"):
        tag = rule[6:].lower()
        if tag == "private":
            return rule if ip.is_private else None
        if not geo:
            return None
        return rule if any(ip.version == n.version and ip in n for n in geo.cidrs(tag)) else None
    try:
        return rule if ip in ipaddress.ip_network(rule, strict=False) else None
    except ValueError:
        return None
