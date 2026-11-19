"""Схема настроек: что владелец меняет без правки кода. Значения в SQLite, изменения в журнале."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .db import DB


@dataclass(frozen=True)
class Field:
    key: str
    group: str
    label: str
    kind: str  # str | text | int | bool
    default: object
    lo: int | None = None
    hi: int | None = None
    hint: str = ""


GROUPS = {
    "panel": "Панель",
    "collector": "Сбор данных",
    "new": "Новые серверы",
    "clients": "Клиенты",
    "security": "Вход",
    "happ": "Happ (конфиги для клиентов)",
    "routing": "Маршрутизация",
    "updates": "Обновления",
}

FIELDS: list[Field] = [
    Field("panel.brand", "panel", "Название в шапке", "str", "lerk-vpn", 1, 40),
    Field("panel.public_url", "panel", "Публичный адрес панели", "str", "", 0, 120, "для ссылок подписки, например https://vpn.example.com; пусто = по адресу, с которого открыта панель"),
    Field("collector.interval_min", "collector", "Опрос серверов, минут", "int", 5, 1, 60, "как часто снимать состояние и трафик"),
    Field("collector.retention_days", "collector", "Хранить историю, дней", "int", 365, 30, 3650),
    Field("new.default_sni", "new", "Маскировка (SNI) для новых серверов", "str", "dl.google.com", 3, 80, "сайт с TLS 1.3, подставляется в новые серверы"),
    Field("new.default_swap_gb", "new", "Swap на новых серверах, ГБ", "int", 4, 0, 64),
    Field("new.install_now", "new", "Устанавливать Xray сразу при добавлении", "bool", True),
    Field("clients.sleep_days", "clients", "Спящий клиент: дней без трафика", "int", 14, 1, 365),
    Field("clients.heavy_gb", "clients", "Подсвечивать клиента от, ГБ за период", "int", 0, 0, 100000, "0 = не подсвечивать"),
    Field("happ.title", "happ", "Название подписки в Happ", "str", "lerk-vpn", 1, 25, "до 25 символов, видно клиенту"),
    Field("happ.title_with_client", "happ", "Добавлять имя клиента к названию", "bool", False),
    Field("happ.update_hours", "happ", "Обновлять подписку в Happ каждые, часов", "int", 12, 1, 168),
    Field("happ.support_url", "happ", "Ссылка на поддержку", "str", "", 0, 200, "например https://t.me/ваш_аккаунт, кнопка в Happ"),
    Field("happ.web_url", "happ", "Ссылка на сайт", "str", "", 0, 200),
    Field("happ.announce", "happ", "Объявление в Happ", "str", "", 0, 200, "до 200 символов, показывается клиенту в приложении"),
    Field("happ.routing_on", "happ", "Отдавать профиль маршрутизации", "bool", True, hint="правила вместе с подпиской, у клиента включаются сами"),
    Field("happ.routing_name", "happ", "Название профиля маршрутизации", "str", "lerk-vpn", 1, 40),
    Field("happ.direct_ru", "routing", "Домены .ru, .su, .рф напрямую", "bool", True),
    Field("happ.geo_ru", "routing", "Всё, что находится в России, напрямую", "bool", True, hint="geoip:ru (адреса РФ) и geosite:category-ru (российские сайты)"),
    Field("happ.ru_services", "routing", "Российские сервисы напрямую", "bool", True, hint="Яндекс, VK, Mail.ru, Сбер, Т-Банк, Ozon, Wildberries, госуслуги, банки и др."),
    Field("happ.block_ads", "routing", "Блокировать рекламу", "bool", False),
    Field("happ.geo_auto", "routing", "Раз в неделю обновлять geo-базы", "bool", True),
    Field("happ.direct_sites", "routing", "Напрямую: сайты", "text", "", 0, 4000, "по одному в строке: domain:example.com, geosite:тег"),
    Field("happ.direct_ips", "routing", "Напрямую: адреса", "text", "", 0, 4000, "по одному в строке: 203.0.113.0/24, geoip:by"),
    Field("happ.proxy_sites", "routing", "Через VPN: сайты", "text", "", 0, 4000, "исключения из правил «напрямую»"),
    Field("happ.proxy_ips", "routing", "Через VPN: адреса", "text", "", 0, 4000),
    Field("happ.block_sites", "routing", "Блокировать: сайты", "text", "", 0, 4000),
    Field("happ.block_ips", "routing", "Блокировать: адреса", "text", "", 0, 4000),
    Field("happ.geoip_url", "routing", "Адрес geoip.dat", "str", "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat", 8, 300),
    Field("happ.geosite_url", "routing", "Адрес geosite.dat", "str", "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat", 8, 300),
    Field("updates.auto_check", "updates", "Раз в сутки проверять обновления", "bool", True),
    Field("security.session_days", "security", "Срок входа, дней", "int", 30, 1, 90),
    Field("security.max_attempts", "security", "Неверных паролей до паузы", "int", 8, 3, 30),
    Field("security.pause_min", "security", "Пауза после неверных паролей, минут", "int", 15, 1, 720),
]
BY_KEY = {f.key: f for f in FIELDS}


LIST_KEYS = ("happ.direct_sites", "happ.direct_ips", "happ.proxy_sites", "happ.proxy_ips", "happ.block_sites", "happ.block_ips")


class Settings:
    def __init__(self, db: DB, geo=None):
        self.db = db
        self.geo = geo

    def get(self, key: str):
        f = BY_KEY[key]
        row = self.db.one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return f.default
        return self._parse(f, row["value"], f.default)

    def all(self) -> dict[str, object]:
        stored = {r["key"]: r["value"] for r in self.db.q("SELECT key,value FROM settings")}
        return {f.key: (self._parse(f, stored[f.key], f.default) if f.key in stored else f.default) for f in FIELDS}

    @staticmethod
    def _parse(f: Field, raw: str, fallback):
        try:
            if f.kind == "int":
                return int(raw)
            if f.kind == "bool":
                return raw == "1"
            return raw
        except ValueError:
            return fallback

    def validate(self, f: Field, raw: str):
        """Возвращает значение нужного типа или бросает ValueError с текстом для человека."""
        raw = (raw or "").strip()
        if f.kind == "int":
            try:
                v = int(raw)
            except ValueError:
                raise ValueError(f"{f.label}: нужно целое число") from None
            if (f.lo is not None and v < f.lo) or (f.hi is not None and v > f.hi):
                raise ValueError(f"{f.label}: от {f.lo} до {f.hi}")
            return v
        if f.kind == "bool":
            return raw in ("1", "on", "true", "yes")
        if len(raw) < (f.lo or 0) or len(raw) > (f.hi or 10_000):
            raise ValueError(f"{f.label}: длина от {f.lo or 0} до {f.hi}")
        if f.key == "panel.public_url" and raw and not raw.startswith(("http://", "https://")):
            raise ValueError("Публичный адрес должен начинаться с http:// или https://")
        if f.key in LIST_KEYS:
            from .happ import TOKEN_RE, split_list

            items = split_list(raw)
            if len(items) > 200:
                raise ValueError(f"{f.label}: не больше 200 правил")
            for x in items:
                if not TOKEN_RE.match(x):
                    raise ValueError(f"{f.label}: «{x[:40]}» не похоже на правило (допустимы буквы, цифры, : . / - _ *)")
                if self.geo and self.geo.loaded() and ":" in x and x.split(":", 1)[0] in ("geosite", "geoip"):
                    kind, tag = x.split(":", 1)
                    if tag.lower() != "private" and not self.geo.has_tag(kind, tag):
                        raise ValueError(f"{f.label}: тега «{x}» нет в geo-базе (найдите нужный в каталоге тегов)")
            return "\n".join(dict.fromkeys(items))
        if f.key in ("happ.geoip_url", "happ.geosite_url") and not raw.startswith("https://"):
            raise ValueError(f"{f.label}: адрес должен начинаться с https://")
        if f.key in ("happ.support_url", "happ.web_url") and raw and not raw.startswith(("http://", "https://")):
            raise ValueError(f"{f.label}: должна начинаться с http:// или https://")
        if f.key == "new.default_sni" and not all(ch.isalnum() or ch in ".-" for ch in raw):
            raise ValueError("SNI: только буквы, цифры, точки и дефисы")
        return raw

    def save(self, form: dict[str, str], ip: str) -> tuple[list[str], list[str]]:
        """Сохраняет присланные поля. Возвращает (изменённые ключи, ошибки)."""
        current = self.all()
        changed, errors = [], []
        for f in FIELDS:
            if f.kind == "bool":
                scope = [g for g in form.get("_scope", "").split(",") if g]
                if f.key not in form and f.group not in scope:
                    continue  # галочка не пришла и форма её группу не охватывает: не считаем снятой
                raw = "1" if form.get(f.key) else "0"
            elif f.key in form:
                raw = form[f.key]
            else:
                continue
            try:
                value = self.validate(f, raw)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if value == current[f.key]:
                continue
            store = ("1" if value else "0") if f.kind == "bool" else str(value)
            if value == f.default:
                self.db.x("DELETE FROM settings WHERE key=?", (f.key,))
            else:
                self.db.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f.key, store))
            old = ("1" if current[f.key] else "0") if f.kind == "bool" else str(current[f.key])
            self.db.x("INSERT INTO settings_log(ts,key,old,new,ip) VALUES(?,?,?,?,?)", (int(time.time()), f.key, old, store, ip))
            changed.append(f.key)
        if any(k.startswith("happ.") for k in changed):
            self.db.kv_set("happ.routing_updated", str(int(time.time())))
        return changed, errors
