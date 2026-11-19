"""Каркас страниц и мелкие компоненты (графики рисуются SVG на сервере, без библиотек)."""

from __future__ import annotations

import html
from urllib.parse import urlencode

from .periods import KEYS, Period, compare

NAV = [
    ("pulse", "Пульс"),
    ("clients", "Клиенты"),
    ("happ", "Конфиги Happ"),
    ("routing", "Маршрутизация"),
    ("servers", "Серверы и каскады"),
    ("system", "Система"),
    ("settings", "Настройки"),
    ("audit", "Журнал"),
]


ICONS = {
    "pulse": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "clients": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "happ": '<rect x="6" y="2" width="12" height="20" rx="3"/><line x1="12" y1="18" x2="12.01" y2="18"/>',
    "routing": '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M6 8.5v3a3 3 0 0 0 3 3h6a3 3 0 0 1 3 3"/>',
    "servers": '<rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><line x1="6" y1="7" x2="6.01" y2="7"/><line x1="6" y1="17" x2="6.01" y2="17"/>',
    "system": '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M19 9h3M19 14h3M2 9h3M2 14h3"/>',
    "settings": '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>',
    "audit": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><polyline points="14 3 14 8 19 8"/><line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="15" y2="17"/>',
    "logo": '<path d="M12 2.5l8 3v6c0 5-3.4 8.7-8 10.5-4.6-1.8-8-5.5-8-10.5v-6z"/><path d="M8.5 12l2.5 2.5L15.5 10"/>',
    "search": '<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>',
    "moon": '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
}


def icon(name: str, cls: str = "") -> str:
    c = f' class="{cls}"' if cls else ""
    return f'<svg{c} viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>'


def _asset_version() -> str:
    """Метка версии статики: после обновления панели браузер сразу берёт новые файлы."""
    import hashlib
    from pathlib import Path

    h = hashlib.sha1()
    for f in sorted((Path(__file__).parent / "static").glob("*")):
        h.update(f.read_bytes())
    return h.hexdigest()[:8]


ASSET_V = _asset_version()


SHORT = {"happ": "Happ", "routing": "Правила", "servers": "Серверы", "settings": "Опции"}


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def url(page: str, **params) -> str:
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    return "/?" + urlencode({"p": page, **clean})


def layout(ctx, page: str, title: str, body: str, counts: dict | None = None) -> str:
    counts = counts or {}
    nav = []
    for key, label in NAV:
        n = counts.get(key)
        badge = f'<span class="cnt">{n}</span>' if n else ""
        on = " on" if key == page or (page in ("client", "job", "search", "qr") and key == {"client": "clients", "job": "servers", "search": "clients", "qr": "happ"}.get(page)) else ""
        nav.append(f'<a class="{on.strip()}" href="{url(key)}"{" aria-current=\"page\"" if on else ""}>{icon(key)}<span class="t"><span class="l">{esc(label)}</span><span class="m">{esc(SHORT.get(key, label))}</span></span>{badge}</a>')
    brand = esc(ctx.settings.get("panel.brand"))
    flash = ""
    if ctx.flash:
        kind, text = ctx.flash
        flash = f'<div class="flash {esc(kind)}" role="status">{esc(text)}</div>'
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{esc(title)} · {brand}</title>
<link rel="stylesheet" href="/static/admin.css?v={ASSET_V}"><link rel="icon" href="data:,">
<script src="/static/admin.js?v={ASSET_V}"></script><script src="/static/admin-dnd.js?v={ASSET_V}" defer></script></head>
<body><div class="layout"><aside class="side"><a class="brand" href="{url('pulse')}"><span class="logo">{icon('logo')}</span>{brand}</a>
<nav class="nav" aria-label="Разделы">{''.join(nav)}</nav>
<div class="side-foot"><span class="who"><span class="avatar">{esc((ctx.actor or 'a')[:1].upper())}</span>{esc(ctx.actor)}</span></div></aside>
<main class="main"><div class="top">
<form class="search" method="get" action="/"><input type="hidden" name="p" value="search">{icon('search')}
<label class="sr" for="q">Поиск</label><input id="q" type="search" name="q" placeholder="Поиск" autocomplete="off"><kbd>/</kbd></form><span class="sp"></span>
<button class="iconbtn theme" id="theme" type="button" aria-label="Сменить тему" title="Тема">{icon('sun','sun')}{icon('moon','moon')}</button>
<form method="post" action="/logout" class="inline"><input type="hidden" name="_csrf" value="{esc(ctx.csrf)}"><button class="iconbtn" type="submit" aria-label="Выйти" title="Выйти">{icon('logout')}</button></form></div>
{flash}{body}</main></div></body></html>"""


def login_page(error: str = "", blocked: int = 0, brand: str = "lerk-vpn") -> str:
    msg = ""
    if blocked:
        msg = f'<div class="flash err">Слишком много попыток. Повторите через {blocked // 60 + 1} мин.</div>'
    elif error:
        msg = f'<div class="flash err">{esc(error)}</div>'
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Вход · {esc(brand)}</title><link rel="stylesheet" href="/static/admin.css?v={ASSET_V}"><script src="/static/admin.js?v={ASSET_V}"></script></head>
<body><div class="login-wrap"><div class="login"><div class="head"><span class="logo">{icon('logo')}</span><h1>{esc(brand)}</h1><span class="muted">Панель управления</span></div>{msg}
<form class="form card" method="post" action="/login">
<label>Логин<input type="text" name="login" autocomplete="username" autofocus required></label>
<label>Пароль<input type="password" name="password" autocomplete="current-password" required></label>
<button class="btn" type="submit">Войти</button></form></div></div></body></html>"""


def disabled_page() -> str:
    return """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="robots" content="noindex"><title>Панель выключена</title></head>
<body style="font-family:system-ui;max-width:520px;margin:15vh auto;padding:0 16px"><h1>Панель выключена</h1>
<p>Не задан хеш пароля администратора. Задайте его на сервере: <code>lerk-vpn web set-password</code>.</p></body></html>"""


def form(ctx, action: str, inner: str, cls: str = "form", extra: str = "") -> str:
    return f'<form class="{cls}" method="post" action="/do/{esc(action)}" {extra}><input type="hidden" name="_csrf" value="{esc(ctx.csrf)}">{inner}</form>'


def button_form(ctx, action: str, label: str, fields: dict | None = None, cls: str = "btn ghost sm", confirm: str = "") -> str:
    hidden = "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">' for k, v in (fields or {}).items())
    conf = f' data-confirm="{esc(confirm)}"' if confirm else ""
    return f'<form class="inline" method="post" action="/do/{esc(action)}"><input type="hidden" name="_csrf" value="{esc(ctx.csrf)}">{hidden}<button class="{cls}" type="submit"{conf}>{esc(label)}</button></form>'


def btnrow(*parts: str) -> str:
    """Ряд кнопок одного размера с ровными отступами."""
    return '<div class="btnrow">' + "".join(parts) + "</div>"


def period_tabs(page: str, current: str, **params) -> str:
    tabs = []
    for key, label in KEYS:
        on = ' class="on"' if key == current else ""
        tabs.append(f'<a{on} href="{url(page, per=key, **params)}">{esc(label)}</a>')
    return f'<div class="tabs" role="tablist">{"".join(tabs)}</div>'


def sparkline(values: list[float | int], w: int = 220, h: int = 38) -> str:
    if not values or max(values) <= 0:
        return f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true"><line x1="0" y1="{h-2}" x2="{w}" y2="{h-2}" stroke="currentColor" opacity=".2"/></svg>'
    n = len(values)
    top = max(values)
    pts = []
    for i, v in enumerate(values):
        x = 0 if n == 1 else i * (w - 2) / (n - 1) + 1
        y = h - 3 - (v / top) * (h - 8)
        pts.append((x, y))
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = line + f" L{pts[-1][0]:.1f},{h} L{pts[0][0]:.1f},{h} Z"
    return f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true"><path class="area" d="{area}"/><path d="{line}"/></svg>'


def bars(values: list[int], labels: list[str], fmt, w: int = 560, h: int = 120) -> str:
    if not values or max(values) <= 0:
        return '<div class="empty">нет данных за период</div>'
    n, top = len(values), max(values)
    gap = 3
    bw = max(2.0, (w - gap * (n - 1)) / n)
    out = [f'<svg class="bars" width="100%" viewBox="0 0 {w} {h + 18}" role="img" aria-label="График по времени">']
    for i, v in enumerate(values):
        bh = max(1.0, v / top * h) if v else 0
        x = i * (bw + gap)
        out.append(f'<rect x="{x:.1f}" y="{h - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="2"><title>{esc(labels[i])}: {esc(fmt(v))}</title></rect>')
    if n:
        out.append(f'<text x="0" y="{h + 14}" font-size="11" fill="currentColor" opacity=".6">{esc(labels[0])}</text>')
        out.append(f'<text x="{w}" y="{h + 14}" font-size="11" text-anchor="end" fill="currentColor" opacity=".6">{esc(labels[-1])}</text>')
    out.append("</svg>")
    return "".join(out)


def hbar(value: int, top: int, w: int = 120) -> str:
    fill = 0 if top <= 0 else max(2, int(value / top * w)) if value else 0
    return f'<svg width="{w}" height="8" viewBox="0 0 {w} 8" aria-hidden="true"><rect width="{w}" height="8" rx="4" fill="currentColor" opacity=".1"/><rect class="bar-h" width="{fill}" height="8" rx="4"/></svg>'


def tile(key: str, title: str, value: str, sub: str = "", spark: str = "", cmp_html: str = "") -> str:
    return f'<section class="card tile" data-key="{esc(key)}"><span class="drag-handle" aria-hidden="true">⠿</span><h3>{esc(title)}</h3><div class="val">{value}</div><div class="sub">{sub}{cmp_html}</div>{spark}</section>'


def cmp_html(cur, prev, p: Period, lower_better: bool = False) -> str:
    if not p.has_prev:
        return ""
    text, cls = compare(cur, prev, lower_better)
    return f' <span class="{cls}">{esc(text)}</span>'


def badge(text: str, kind: str = "") -> str:
    return f'<span class="badge {kind}">{esc(text)}</span>'


def block(key: str, title: str, inner: str, extra: str = "") -> str:
    return f'<section class="card" data-key="{esc(key)}"><span class="drag-handle" aria-hidden="true">⠿</span><h2>{esc(title)}</h2>{extra}{inner}</section>'


def table(header: list[str], rows: list[list[str]], num: set[int] | None = None, empty: str = "нет данных") -> str:
    num = num or set()
    if not rows:
        return f'<div class="empty">{esc(empty)}</div>'
    head = "".join(f'<th class="{"num" if i in num else ""}">{esc(h)}</th>' for i, h in enumerate(header))
    import re as _re

    def cell(i: int, c: str) -> str:
        short = len(_re.sub(r"<[^>]*>", "", str(c))) <= 30
        cls = ("num " if i in num else "") + ("nw" if short else "")
        return f'<td class="{cls.strip()}">{c}</td>'

    body = "".join("<tr>" + "".join(cell(i, c) for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def pager(page: str, n: int, per_page: int, current: int, **params) -> str:
    pages = (n + per_page - 1) // per_page
    if pages <= 1:
        return ""
    items = []
    for i in range(1, pages + 1):
        items.append(f"<span>{i}</span>" if i == current else f'<a href="{url(page, pg=i, **params)}">{i}</a>')
    return f'<div class="pager">{"".join(items)}</div>'
