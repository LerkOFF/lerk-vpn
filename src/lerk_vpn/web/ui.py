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
        nav.append(f'<a class="{on.strip()}" href="{url(key)}">{esc(label)}{badge}</a>')
    brand = esc(ctx.settings.get("panel.brand"))
    flash = ""
    if ctx.flash:
        kind, text = ctx.flash
        flash = f'<div class="flash {esc(kind)}" role="status">{esc(text)}</div>'
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{esc(title)} · {brand}</title>
<link rel="stylesheet" href="/static/admin.css"><link rel="icon" href="data:,">
<script src="/static/admin.js"></script><script src="/static/admin-dnd.js" defer></script></head>
<body><div class="layout"><aside class="side"><a class="brand" href="{url('pulse')}">{brand}</a>
<nav class="nav" aria-label="Разделы">{''.join(nav)}</nav></aside>
<main class="main"><div class="top">
<form class="search" method="get" action="/"><input type="hidden" name="p" value="search">
<label class="sr" for="q">Поиск</label><input id="q" type="search" name="q" placeholder="Поиск: клиент, сервер, цепочка  (/ или ⌘K)" autocomplete="off"></form>
<button class="theme" id="theme" type="button" aria-label="Сменить тему">Тема</button>
<form method="post" action="/logout" class="inline"><input type="hidden" name="_csrf" value="{esc(ctx.csrf)}"><button class="btn ghost sm" type="submit">Выйти</button></form></div>
{flash}{body}</main></div></body></html>"""


def login_page(error: str = "", blocked: int = 0, brand: str = "lerk-vpn") -> str:
    msg = ""
    if blocked:
        msg = f'<div class="flash err">Слишком много попыток. Повторите через {blocked // 60 + 1} мин.</div>'
    elif error:
        msg = f'<div class="flash err">{esc(error)}</div>'
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Вход · {esc(brand)}</title><link rel="stylesheet" href="/static/admin.css"><script src="/static/admin.js"></script></head>
<body><div class="login"><h1>{esc(brand)}</h1>{msg}
<form class="form card" method="post" action="/login">
<label>Логин<input type="text" name="login" autocomplete="username" autofocus required></label>
<label>Пароль<input type="password" name="password" autocomplete="current-password" required></label>
<button class="btn" type="submit">Войти</button></form></div></body></html>"""


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
    body = "".join("<tr>" + "".join(f'<td class="{"num" if i in num else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def pager(page: str, n: int, per_page: int, current: int, **params) -> str:
    pages = (n + per_page - 1) // per_page
    if pages <= 1:
        return ""
    items = []
    for i in range(1, pages + 1):
        items.append(f"<span>{i}</span>" if i == current else f'<a href="{url(page, pg=i, **params)}">{i}</a>')
    return f'<div class="pager">{"".join(items)}</div>'
