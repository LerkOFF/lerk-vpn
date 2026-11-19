"""Конфиги для Happ: подписка с заголовками приложения, профиль маршрутизации, глубокие ссылки."""

from __future__ import annotations

import base64
import json
import re
import time

GEOIP_URL = "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat"
GEOSITE_URL = "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat"
PRIVATE_IP = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "224.0.0.0/4", "255.255.255.255"]
RU_SITES = ["domain:ru", "domain:su", "domain:xn--p1ai"]
TOKEN_RE = re.compile(r"^[A-Za-z0-9:._/*-]{1,80}$")


def split_list(text: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,\n;]+", text or "") if t.strip()]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _present(geo, kind: str, tags: list[str]) -> list[str]:
    if geo is None or not geo.loaded():
        return tags
    return [t for t in tags if geo.has_tag(kind, t)]


def _uniq(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def routing_profile(s: dict, updated: int | None = None, geo=None) -> dict:
    """Профиль маршрутизации Happ: что идёт напрямую, что через VPN, что блокируется."""
    from .geo import RU_SERVICE_TAGS

    sites: list[str] = []
    if s["happ.direct_ru"]:
        sites += RU_SITES
    if s["happ.geo_ru"]:
        sites += [f"geosite:{t}" for t in _present(geo, "geosite", ["category-ru"])]
    if s["happ.ru_services"]:
        sites += [f"geosite:{t}" for t in _present(geo, "geosite", [t for t in RU_SERVICE_TAGS if t != "category-ru"])]
    sites += split_list(s["happ.direct_sites"])
    ips = list(PRIVATE_IP)
    if s["happ.geo_ru"]:
        ips += ["geoip:ru"]
    ips += split_list(s["happ.direct_ips"])
    block_sites = (["geosite:category-ads-all"] if s["happ.block_ads"] else []) + split_list(s["happ.block_sites"])
    return {
        "Name": s["happ.routing_name"],
        "GlobalProxy": "true",
        "RemoteDNSType": "DoH",
        "RemoteDNSDomain": "https://cloudflare-dns.com/dns-query",
        "RemoteDNSIP": "1.1.1.1",
        "DomesticDNSType": "DoH",
        "DomesticDNSDomain": "https://dns.google/dns-query",
        "DomesticDNSIP": "8.8.8.8",
        "Geoipurl": s["happ.geoip_url"],
        "Geositeurl": s["happ.geosite_url"],
        "LastUpdated": str(updated or int(time.time())),
        "DnsHosts": {"cloudflare-dns.com": "1.1.1.1", "dns.google": "8.8.8.8"},
        "DirectSites": _uniq(sites),
        "DirectIp": _uniq(ips),
        "ProxySites": _uniq(split_list(s["happ.proxy_sites"])),
        "ProxyIp": _uniq(split_list(s["happ.proxy_ips"])),
        "BlockSites": _uniq(block_sites),
        "BlockIp": _uniq(split_list(s["happ.block_ips"])),
        "DomainStrategy": "IPIfNonMatch",
        "FakeDNS": "false",
    }


def routing_link(profile: dict, activate: bool = True) -> str:
    raw = json.dumps(profile, ensure_ascii=False, indent=2).encode()
    return f"happ://routing/{'onadd' if activate else 'add'}/{b64(raw)}"


def add_link(sub_url: str) -> str:
    return f"happ://add/{sub_url}"


def header_text(value: str, limit: int) -> str:
    """Значение заголовка: ASCII как есть, остальное в base64 (формат Happ)."""
    value = value[:limit]
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return "base64:" + b64(value.encode())


def subscription_headers(s: dict, client: str, up: int, down: int, updated: int | None = None, geo=None) -> dict[str, str]:
    title = f"{s['happ.title']} {client}".strip()[:25] if s["happ.title_with_client"] else s["happ.title"]
    headers = {
        "profile-title": header_text(title, 25),
        "profile-update-interval": str(max(1, int(s["happ.update_hours"]))),
        "subscription-userinfo": f"upload={up}; download={down}; total=0; expire=0",
        "content-disposition": f'attachment; filename="{re.sub(r"[^A-Za-z0-9._-]", "_", client)}"',
    }
    if s["happ.support_url"]:
        headers["support-url"] = s["happ.support_url"]
    if s["happ.web_url"]:
        headers["profile-web-page-url"] = s["happ.web_url"]
    if s["happ.announce"]:
        headers["announce"] = header_text(s["happ.announce"], 200) if s["happ.announce"].isascii() else header_text(s["happ.announce"], 200)
    if s["happ.routing_on"]:
        headers["routing"] = routing_link(routing_profile(s, updated, geo))
    else:
        headers["routing-enable"] = "0"
    return headers


def client_message(sub_url: str, brand: str, with_deeplink: bool = True) -> str:
    lines = [
        "Ваш доступ к VPN готов.",
        "",
        "1. Установите приложение Happ (App Store, Google Play, happ.su).",
        f"2. Откройте это в Happ: {add_link(sub_url) if with_deeplink else sub_url}",
        f"   или добавьте подписку вручную, адрес: {sub_url}",
        "3. Выберите сервер и нажмите кнопку подключения.",
    ]
    return "\n".join(lines)
