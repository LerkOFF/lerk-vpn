"""Страницы панели."""

from __future__ import annotations

import json
import os
import platform
import shutil
import time
from pathlib import Path

from .. import __version__
from ..model import FINGERPRINTS, SpecError
from ..xraycfg import client_links
from . import data, geo as geomod, happ, qr
from .periods import MSK, KEYS, fmt_ago, fmt_bytes, fmt_dt, get_period
from .settings import BY_KEY, FIELDS, GROUPS
from .ui import (badge, bars, block, button_form, cmp_html, esc, form, hbar, layout, pager, period_tabs,
                 sparkline, table, tile, url)


class NotFound(Exception):
    pass


STATUS_LABEL = {"running": "выполняется", "ok": "готово", "error": "ошибка"}
ROLE_LABEL = {"entry": "Сервер 1 (вход)", "exit": "Сервер 2 (выход)", "": "роль не задана"}


def _state(ctx):
    topo, clients, state = ctx.ws.load()
    state.ensure(topo, clients)
    return topo, clients, state


def nav_counts(ctx) -> dict:
    counts: dict[str, int] = {}
    try:
        topo, _, _ = ctx.ws.load()
    except SpecError:
        return counts
    interval = ctx.app.collector.interval()
    bad = 0
    for name in topo.nodes:
        label, kind = data.server_state(data.latest_sample(ctx.db, name), interval=interval)
        if kind in ("bad", "warn"):
            bad += 1
    if ctx.db.kv_get("dirty") == "1":
        bad += 1
    if bad:
        counts["servers"] = bad
    _, kind = data.collector_state(ctx.db, interval)
    if kind in ("bad", "warn"):
        counts["system"] = 1
    return counts


def render(ctx, page: str, title: str, body: str) -> str:
    return layout(ctx, page, title, body, nav_counts(ctx))


def pending_banner(ctx) -> str:
    if ctx.db.kv_get("dirty") != "1":
        return ""
    return '<div class="flash info banner"><span>Есть изменения, которые ещё не применены на серверах.</span>' + button_form(ctx, "apply", "Применить на серверах", cls="btn sm") + "</div>"


# ---------------------------------------------------------------- пульс

def pulse(ctx) -> str:
    topo, clients, state = _state(ctx)
    p = get_period(ctx.q.get("per", "7d"))
    db = ctx.db
    cur = data.traffic_total(db, p.start, p.end)
    prev = data.traffic_total(db, p.prev_start, p.prev_end) if p.has_prev else None
    vals, starts = data.traffic_series(db, p)
    act = data.active_clients(db, p.start, p.end)
    act_prev = data.active_clients(db, p.prev_start, p.prev_end) if p.has_prev else None
    enabled = sum(1 for c in clients if c.enabled)
    interval = ctx.app.collector.interval()
    states = [data.server_state(data.latest_sample(db, n), interval=interval) for n in topo.nodes]
    up = sum(1 for _, k in states if k == "good")
    ver = data.latest_verify(db)
    ver_ok = sum(1 for c in topo.chains if ver.get(c, (False,))[0])
    ver_known = sum(1 for c in topo.chains if c in ver)

    tiles = [
        tile("traffic", "Трафик клиентов", esc(fmt_bytes(cur)), "за период", sparkline(vals), cmp_html(cur, prev, p)),
        tile("active", "Клиентов с трафиком", esc(act if act is not None else "нет данных"), "за период", "", cmp_html(act, act_prev, p)),
        tile("clients", "Клиентов", f"{enabled} из {len(clients)}", "включено сейчас"),
        tile("servers", "Серверы", f'<span class="{"good" if up == len(states) and states else "bad"}">{up} из {len(states)}</span>', "работают и совпадают с описанием"),
        tile("chains", "Каскады проверены", f"{ver_ok} из {len(topo.chains)}" if ver_known else "нет данных", "последняя сквозная проверка" if ver_known else 'запустите «Проверить» на странице серверов'),
    ]
    top = sorted(data.per_client(db, p.start, p.end).items(), key=lambda kv: -kv[1]["bytes"])[:8]
    top_n = top[0][1]["bytes"] if top else 0
    top_rows = [[f'<a href="{url("client", name=n)}">{esc(n)}</a>', esc(fmt_bytes(v["bytes"])), hbar(v["bytes"], top_n), esc(fmt_ago(v["last"]))] for n, v in top]
    chain_tr = data.per_chain(db, p.start, p.end)
    chain_rows = []
    for ch in topo.chains.values():
        v = ver.get(ch.name)
        status = badge("проверена", "good") if v and v[0] else badge("не прошла", "bad") if v else badge("не проверена")
        chain_rows.append([esc(ch.name), esc(" → ".join(ch.path)), esc(fmt_bytes(chain_tr.get(ch.name, 0) if cur is not None else None)), status])
    events = ctx.db.q("SELECT ts,actor,action,target FROM audit ORDER BY id DESC LIMIT 8")
    ev_rows = [[esc(fmt_dt(e["ts"])), esc(e["action"]), esc(e["target"] or "")] for e in events]
    body = (
        f"<h1>Пульс</h1>{pending_banner(ctx)}{period_tabs('pulse', p.key)}"
        f'<div class="grid" data-sortable="tiles">{"".join(tiles)}</div>'
        f'<div class="cols" data-sortable="blocks">'
        + block("top", "Топ клиентов", table(["Клиент", "Трафик", "", "Последний трафик"], top_rows, empty="за период трафика нет"))
        + block("chains", "Каскады", table(["Каскад", "Путь", "Трафик", "Проверка"], chain_rows, empty="каскадов нет"))
        + block("events", "Последние действия", table(["Когда", "Действие", "Над чем"], ev_rows, empty="журнал пуст"))
        + '</div><p><button class="btn ghost sm" type="button" data-dnd-reset>Сбросить порядок блоков</button></p>'
    )
    return render(ctx, "pulse", "Пульс", body)


# ---------------------------------------------------------------- клиенты

def clients_page(ctx) -> str:
    topo, clients, state = _state(ctx)
    p = get_period(ctx.q.get("per", "30d"))
    q = ctx.q.get("q", "").strip().lower()
    chain_f, st_f, sort = ctx.q.get("chain", ""), ctx.q.get("st", ""), ctx.q.get("sort", "name")
    sleep_days = int(ctx.settings.get("clients.sleep_days"))
    heavy = int(ctx.settings.get("clients.heavy_gb")) * 1024**3
    usage = data.per_client(ctx.db, p.start, p.end)
    seen = data.last_seen(ctx.db)
    now = int(time.time())

    def is_sleeping(c) -> bool:
        last = seen.get(c.name)
        return c.enabled and (last is None or now - last > sleep_days * 86400)

    rows_src = []
    for c in clients:
        if q and q not in c.name.lower() and q not in c.note.lower():
            continue
        if chain_f and chain_f not in c.chains:
            continue
        if st_f == "on" and not c.enabled:
            continue
        if st_f == "off" and c.enabled:
            continue
        if st_f == "sleep" and not is_sleeping(c):
            continue
        rows_src.append(c)
    if sort == "traffic":
        rows_src.sort(key=lambda c: -usage.get(c.name, {}).get("bytes", 0))
    elif sort == "last":
        rows_src.sort(key=lambda c: -(seen.get(c.name) or 0))
    else:
        rows_src.sort(key=lambda c: c.name)
    per_page = 20
    page_no = max(1, int(ctx.q.get("pg", "1") or 1)) if (ctx.q.get("pg", "1") or "1").isdigit() else 1
    chunk = rows_src[(page_no - 1) * per_page: page_no * per_page]
    vals_by_client: dict[str, list[int]] = {}
    for c in chunk:
        vals_by_client[c.name] = data.traffic_series(ctx.db, p, c.name)[0]
    rows = []
    for c in chunk:
        u = usage.get(c.name)
        b = u["bytes"] if u else 0
        flag = ""
        if heavy and b >= heavy:
            flag = " " + badge("много", "warn")
        status = badge("выключен", "warn") if not c.enabled else badge("спит") if is_sleeping(c) else badge("активен", "good")
        rows.append([
            f'<a href="{url("client", name=c.name)}">{esc(c.name)}</a>',
            esc(", ".join(c.chains)), status, esc(fmt_bytes(b)) + flag, esc(fmt_ago(seen.get(c.name))),
            sparkline(vals_by_client[c.name], 100, 24), esc(c.note),
        ])
    params = {"per": p.key, "q": q, "chain": chain_f, "st": st_f, "sort": sort}
    chain_opts = '<option value="">все каскады</option>' + "".join(f'<option value="{esc(k)}"{" selected" if k == chain_f else ""}>{esc(k)}</option>' for k in topo.chains)
    st_opts = "".join(f'<option value="{v}"{" selected" if v == st_f else ""}>{l}</option>' for v, l in (("", "любой статус"), ("on", "включённые"), ("off", "выключенные"), ("sleep", "спящие")))
    sort_opts = "".join(f'<option value="{v}"{" selected" if v == sort else ""}>{l}</option>' for v, l in (("name", "по имени"), ("traffic", "по трафику"), ("last", "по активности")))
    filters = (
        f'<form class="form" method="get" action="/"><input type="hidden" name="p" value="clients"><input type="hidden" name="per" value="{esc(p.key)}">'
        f'<div class="row"><label>Поиск<input type="search" name="q" value="{esc(q)}" placeholder="имя или заметка"></label>'
        f'<label>Каскад<select name="chain">{chain_opts}</select></label><label>Статус<select name="st">{st_opts}</select></label>'
        f'<label>Сортировка<select name="sort">{sort_opts}</select></label><button class="btn" type="submit">Применить</button></div></form>'
    )
    chain_checks = "".join(f'<label><input type="checkbox" name="chains" value="{esc(k)}"{" checked" if len(topo.chains) == 1 else ""}> {esc(ch.title)} <span class="muted">({esc(k)})</span></label>' for k, ch in topo.chains.items())
    if not topo.chains:
        add = '<div class="empty">Сначала добавьте сервер 1 и сервер 2 и создайте каскад на странице «Серверы и каскады».</div>'
    else:
        add = form(
            ctx, "client_add",
            '<div class="row"><label>Имя (латиницей)<input type="text" name="name" required pattern="[a-z0-9][a-z0-9_-]{0,31}" placeholder="ann"></label>'
            '<label>Заметка<input type="text" name="note" maxlength="200" placeholder="кому выдан"></label></div>'
            f'<div class="checks">{chain_checks}</div><div><button class="btn" type="submit">Добавить клиента</button></div>',
        )
    table_html = table(["Клиент", "Каскады", "Статус", "Трафик", "Был трафик", "Динамика", "Заметка"], rows, num={3}, empty="клиентов по фильтру нет")
    body = (
        f"<h1>Клиенты</h1>{pending_banner(ctx)}{period_tabs('clients', p.key, q=q, chain=chain_f, st=st_f, sort=sort)}"
        f'<div class="stack" data-sortable="clients">{block("add", "Новый клиент", add)}{block("list", f"Список ({len(rows_src)})", filters + table_html + pager("clients", len(rows_src), per_page, page_no, **params))}</div>'
        '<p><button class="btn ghost sm" type="button" data-dnd-reset>Сбросить порядок блоков</button></p>'
    )
    return render(ctx, "clients", "Клиенты", body)


def client_page(ctx) -> str:
    topo, clients, state = _state(ctx)
    name = ctx.q.get("name", "")
    client = next((c for c in clients if c.name == name), None)
    if not client:
        raise NotFound()
    p = get_period(ctx.q.get("per", "30d"))
    u = data.per_client(ctx.db, p.start, p.end).get(name)
    prev = data.per_client(ctx.db, p.prev_start, p.prev_end).get(name) if p.has_prev else None
    vals, starts = data.traffic_series(ctx.db, p, name)
    labels = [time.strftime("%d.%m %H:%M" if p.bucket < 86400 else "%d.%m", time.gmtime(s + 3 * 3600)) for s in starts]
    seen = data.last_seen(ctx.db).get(name)
    base = ctx.base_url
    token = state.token(name)
    links = client_links(topo, state, client)
    sub_url = f"{base}/sub/{token}"

    status = badge("включён", "good") if client.enabled else badge("выключен", "warn")
    info = (
        f'<dl class="kv"><dt>Статус</dt><dd>{status}</dd><dt>Каскады</dt><dd>{esc(", ".join(client.chains))}</dd>'
        f'<dt>Последний трафик</dt><dd>{esc(fmt_ago(seen))}</dd>'
        f'<dt>Трафик за период</dt><dd>{esc(fmt_bytes(u["bytes"] if u else (0 if data.has_samples(ctx.db, p.start, p.end) else None)))}{cmp_html(u["bytes"] if u else 0, prev["bytes"] if prev else 0, p)}</dd>'
        f'<dt>Принято / отдано</dt><dd>{esc(fmt_bytes(u["up"] if u else None))} / {esc(fmt_bytes(u["down"] if u else None))}</dd></dl>'
        + form(ctx, "client_note", f'<input type="hidden" name="name" value="{esc(name)}"><div class="row"><label>Заметка<input type="text" name="note" maxlength="200" value="{esc(client.note)}"></label><button class="btn ghost" type="submit">Сохранить</button></div>')
    )
    link_html = f'<h3>Адрес подписки (для Happ и других приложений)</h3><div class="link"><span class="mono" id="suburl">{esc(sub_url)}</span><button class="btn ghost sm" type="button" data-copy="suburl">Копировать</button></div><h3>Ссылки на каскады</h3>'
    for i, link in enumerate(links):
        chain = topo.chains[client.chains[i]]
        link_html += (
            f'<div class="link"><span class="mono" id="lk{i}" data-value="{esc(link)}">{esc(chain.title)}: скрыта</span>'
            f'<span><button class="btn ghost sm" type="button" data-reveal="lk{i}">Показать</button> <button class="btn ghost sm" type="button" data-copy="lk{i}">Копировать</button></span></div>'
        )
    if not client.enabled:
        link_html += '<p class="hint">Клиент выключен: после применения на серверах эти ссылки перестанут работать.</p>'
    chain_checks = "".join(f'<label><input type="checkbox" name="chains" value="{esc(k)}"{" checked" if k in client.chains else ""}> {esc(ch.title)} ({esc(k)})</label>' for k, ch in topo.chains.items())
    chains_form = form(ctx, "client_chains", f'<input type="hidden" name="name" value="{esc(name)}"><div class="checks">{chain_checks}</div><div><button class="btn ghost" type="submit">Сохранить каскады</button></div>')
    actions = (
        button_form(ctx, "client_toggle", "Выключить" if client.enabled else "Включить", {"name": name})
        + " " + button_form(ctx, "client_rotate", "Выпустить новые ключи", {"name": name}, confirm="Старые ссылки перестанут работать. Нажмите ещё раз")
        + " " + button_form(ctx, "client_delete", "Удалить клиента", {"name": name}, cls="btn danger sm", confirm="Удалить насовсем? Нажмите ещё раз")
    )
    happ_html = happ_block(ctx, name, client, links, sub_url, topo)
    hist = ctx.db.q("SELECT ts,action,detail,ip FROM audit WHERE target=? ORDER BY id DESC LIMIT 30", (name,))
    hist_rows = [[esc(fmt_dt(h["ts"])), esc(h["action"]), esc(h["detail"] or ""), esc(h["ip"] or "")] for h in hist]
    body = (
        f'<p><a href="{url("clients")}">← все клиенты</a></p><h1>Клиент {esc(name)}</h1>{pending_banner(ctx)}{period_tabs("client", p.key, name=name)}'
        '<div class="cols" data-sortable="card">'
        + block("info", "Данные", info) + block("happ", "Конфиг для Happ", happ_html) + block("access", "Доступ", link_html)
        + block("traffic", "Трафик по времени", bars(vals, labels, fmt_bytes))
        + block("chains", "Каскады клиента", chains_form) + block("actions", "Действия", actions)
        + block("history", "История", table(["Когда", "Что", "Подробности", "Адрес"], hist_rows, empty="записей нет"))
        + '</div><p><button class="btn ghost sm" type="button" data-dnd-reset>Сбросить порядок блоков</button></p>'
    )
    return render(ctx, "client", f"Клиент {name}", body)


def happ_block(ctx, name: str, client, links: list[str], sub_url: str, topo) -> str:
    if not client.enabled:
        return '<p class="muted">Клиент выключен, конфиг не выдаётся. Включите клиента и примените изменения.</p>'
    s = ctx.settings.all()
    deep = happ.add_link(sub_url)
    msg = happ.client_message(sub_url, s["panel.brand"])
    out = (
        f'<p><a class="btn" href="{esc(deep)}">Открыть в Happ</a> <a class="btn ghost" href="{url("qr", name=name)}">Страница для клиента с QR</a></p>'
        f'<div class="qrbox">{qr.svg(sub_url)}</div><p class="hint">Отсканируйте в Happ: «+» → «Сканировать QR». Внутри адрес подписки: в приложении появятся все каскады клиента, список обновится сам.</p>'
        f'<h3>Сообщение клиенту</h3><pre class="pre" id="clmsg">{esc(msg)}</pre><p><button class="btn ghost sm" type="button" data-copy="clmsg">Копировать сообщение</button></p>'
    )
    if s["happ.routing_on"]:
        prof = happ.routing_profile(s, int(ctx.db.kv_get("happ.routing_updated", "0") or 0) or None, ctx.app.geo)
        out += (f'<h3>Маршрутизация</h3><p class="hint">Профиль «{esc(prof["Name"])}» уходит вместе с подпиской и включается в Happ сам. '
                f'Отдельно добавить: <a href="{esc(happ.routing_link(prof))}">happ://routing</a>. Правила меняются в настройках.</p>')
    out += "<h3>QR отдельных каскадов</h3>"
    for i, link in enumerate(links):
        chain = topo.chains[client.chains[i]]
        out += f'<details><summary>{esc(chain.title)} ({esc(chain.name)})</summary><div class="qrbox">{qr.svg(link, "L")}</div></details>'
    return out


def happ_page(ctx) -> str:
    topo, clients, state = _state(ctx)
    s = ctx.settings.all()
    rows = []
    for c in clients:
        sub_url = f"{ctx.base_url}/sub/{state.token(c.name)}"
        if c.enabled:
            actions = (f'<a class="btn sm" href="{esc(happ.add_link(sub_url))}">Открыть в Happ</a> '
                       f'<a class="btn ghost sm" href="{url("qr", name=c.name)}">QR</a> '
                       f'<span class="sr" id="s_{esc(c.name)}">{esc(sub_url)}</span>'
                       f'<button class="btn ghost sm" type="button" data-copy="s_{esc(c.name)}">Копировать адрес</button>')
        else:
            actions = '<span class="muted">клиент выключен</span>'
        rows.append([f'<a href="{url("client", name=c.name)}">{esc(c.name)}</a>', esc(", ".join(c.chains)), badge("включён", "good") if c.enabled else badge("выключен", "warn"), actions])
    prof = happ.routing_profile(s, int(ctx.db.kv_get("happ.routing_updated", "0") or 0) or None, ctx.app.geo)
    routing = (
        f'<p>Профиль «{esc(prof["Name"])}»: напрямую {len(prof["DirectSites"])} правил по сайтам и {len(prof["DirectIp"])} по адресам'
        f'{", блокировка рекламы" if prof["BlockSites"] else ""}. {"Отдаётся вместе с каждой подпиской." if s["happ.routing_on"] else "Сейчас выключен в настройках."}</p>'
        f'<p><a class="btn ghost" href="{esc(happ.routing_link(prof))}">Добавить профиль в Happ на этом устройстве</a></p>'
        f'<pre class="pre">{esc(json.dumps(prof, ensure_ascii=False, indent=2))}</pre><p><a href="{url("settings")}">Изменить правила в настройках</a></p>'
    )
    base_note = '' if ctx.base_url.startswith("https://") else '<div class="flash info">Адрес панели пока без https. Happ принимает подписки с https, задайте «Публичный адрес панели» в настройках, когда домен готов.</div>'
    body = (
        f"<h1>Конфиги Happ</h1>{base_note}{pending_banner(ctx)}"
        '<div class="stack" data-sortable="happ">'
        + block("clients", "Выдача клиентам", table(["Клиент", "Каскады", "Статус", "Конфиг"], rows, empty="клиентов пока нет, добавьте на странице «Клиенты»"))
        + block("routing", "Профиль маршрутизации", routing) + "</div>"
        '<p><button class="btn ghost sm" type="button" data-dnd-reset>Сбросить порядок блоков</button></p>'
    )
    return render(ctx, "happ", "Конфиги Happ", body)


def qr_page(ctx) -> str:
    topo, clients, state = _state(ctx)
    name = ctx.q.get("name", "")
    client = next((c for c in clients if c.name == name and c.enabled), None)
    if not client:
        raise NotFound()
    sub_url = f"{ctx.base_url}/sub/{state.token(name)}"
    steps = (
        "<ol><li>Установите приложение <strong>Happ</strong> (App Store, Google Play, happ.su).</li>"
        "<li>Откройте Happ, нажмите «+» и выберите «Сканировать QR», наведите камеру на код ниже.</li>"
        "<li>Или нажмите кнопку «Открыть в Happ» на телефоне с установленным приложением.</li>"
        "<li>Выберите сервер и нажмите кнопку подключения.</li></ol>"
    )
    body = (
        f'<p><a href="{url("client", name=name)}">← карточка клиента</a></p><h1>Доступ для {esc(name)}</h1>'
        f'<div class="card">{steps}<div class="qrbox">{qr.svg(sub_url, size=280)}</div>'
        f'<p><a class="btn" href="{esc(happ.add_link(sub_url))}">Открыть в Happ</a> <button class="btn ghost" type="button" data-print>Печать</button></p>'
        f'<p class="hint mono">{esc(sub_url)}</p></div>'
    )
    return render(ctx, "qr", f"Доступ {name}", body)


# ---------------------------------------------------------------- серверы и каскады

def servers_page(ctx) -> str:
    topo, clients, state = _state(ctx)
    interval = ctx.app.collector.interval()
    default_install = ctx.settings.get("new.install_now")
    add = form(
        ctx, "server_add",
        '<div class="row"><label>IP-адрес сервера<input type="text" name="host" required placeholder="203.0.113.10" autocomplete="off"></label>'
        '<label>Логин<input type="text" name="user" value="root" required autocomplete="off"></label>'
        '<label>Пароль<input type="password" name="password" autocomplete="new-password" placeholder="нужен один раз, не сохраняется"></label></div>'
        '<div class="checks"><label><input type="radio" name="role" value="entry" checked> Сервер 1 (вход, куда подключаются клиенты)</label>'
        '<label><input type="radio" name="role" value="exit"> Сервер 2 (выход в интернет)</label></div>'
        f'<div class="checks"><label><input type="checkbox" name="install" value="1"{" checked" if default_install else ""}> сразу установить и настроить Xray (swap {esc(ctx.settings.get("new.default_swap_gb"))} ГБ, BBR)</label></div>'
        '<div><button class="btn" type="submit">Добавить сервер</button></div>'
        '<p class="hint">Панель войдёт по паролю, положит свой SSH-ключ и дальше работает по ключу. Пароль нигде не сохраняется и в журнал не попадает.</p>',
        extra='autocomplete="off"',
    )
    rows = []
    for name, n in topo.nodes.items():
        s = data.latest_sample(ctx.db, name)
        label, kind = data.server_state(s, interval=interval)
        used = [c for c, ch in topo.chains.items() if name in ch.path]
        role_sel = "".join(f'<option value="{v}"{" selected" if v == n.role else ""}>{esc(l)}</option>' for v, l in ROLE_LABEL.items())
        role_form = form(ctx, "server_role", f'<input type="hidden" name="name" value="{esc(name)}"><select name="role" aria-label="Роль">{role_sel}</select> <button class="btn ghost sm" type="submit">ОК</button>', cls="inline")
        delete = button_form(ctx, "server_delete", "Удалить", {"name": name}, cls="btn danger sm", confirm="Точно? Нажмите ещё раз") if not used else '<span class="hint">в каскаде</span>'
        rows.append([
            f"<strong>{esc(name)}</strong>", esc(n.host), esc(n.country or "-"), role_form, badge(label, kind),
            esc(s["xray"] if s and s["ok"] else "-"), esc(", ".join(used) or "-"), delete,
        ])
    srv_table = table(["Имя", "Адрес", "Страна", "Роль", "Состояние", "Xray", "Каскады", ""], rows, empty="серверов нет")
    entries = [n for n in topo.nodes.values() if n.role in ("entry", "")]
    exits = [n for n in topo.nodes.values() if n.role in ("exit", "")]
    ent_html = "".join(f'<label><input type="checkbox" name="entries" value="{esc(n.name)}"> {esc(n.name)} · {esc(n.host)} {esc(n.country)}</label>' for n in entries) or '<span class="muted">нет серверов 1</span>'
    exit_html = "".join(f'<label><input type="radio" name="exit" value="{esc(n.name)}"> {esc(n.name)} · {esc(n.host)} {esc(n.country)}</label>' for n in exits) or '<span class="muted">нет серверов 2</span>'
    cascade = form(
        ctx, "cascade_create",
        f'<h3>Серверы 1 (можно несколько)</h3><div class="checks">{ent_html}</div><h3>Сервер 2 (только один)</h3><div class="checks">{exit_html}</div>'
        '<div><button class="btn" type="submit">Создать каскады</button></div><p class="hint">Для каждого выбранного сервера 1 создаётся каскад: клиент → сервер 1 → сервер 2 → интернет.</p>',
    )
    edit_blocks = []
    for name, n in topo.nodes.items():
        fps = "".join(f'<option value="{f}"{" selected" if f == n.fingerprint else ""}>{f}</option>' for f in sorted(FINGERPRINTS))
        edit_blocks.append(
            f'<details><summary><strong>{esc(name)}</strong> · {esc(n.host)} · порт {n.port} · SNI {esc(n.sni)}{" · mux" if n.mux else ""}</summary>'
            + form(
                ctx, "server_edit",
                f'<input type="hidden" name="name" value="{esc(name)}">'
                '<div class="row">'
                f'<label>Порт Xray (куда подключаются клиенты)<input type="number" name="port" min="1" max="65535" value="{n.port}"></label>'
                f'<label>Порт SSH<input type="number" name="ssh_port" min="1" max="65535" value="{n.ssh_port}"></label>'
                f'<label>Swap, ГБ<input type="number" name="swap_gb" min="0" max="64" value="{n.swap_gb}"></label></div>'
                '<div class="row">'
                f'<label>SNI (под какой сайт маскируется Reality)<input type="text" name="sni" value="{esc(n.sni)}"></label>'
                f'<label>Fingerprint<select name="fingerprint">{fps}</select></label>'
                f'<label>Flow<select name="flow"><option value="vision"{" selected" if n.flow else ""}>xtls-rprx-vision (быстрее, по умолчанию)</option>'
                f'<option value="none"{"" if n.flow else " selected"}>без flow (нужен для mux)</option></select></label></div>'
                '<div class="row">'
                f'<label><input type="checkbox" name="mux" value="1"{" checked" if n.mux else ""}> Мультиплексирование (mux) на переходах в этот сервер</label>'
                f'<label>Потоков в mux<input type="number" name="mux_concurrency" min="1" max="1024" value="{n.mux_concurrency}"></label></div>'
                '<div class="checks">'
                f'<label><input type="checkbox" name="block_torrents" value="1"{" checked" if n.block_torrents else ""}> Блокировать торренты на выходе</label>'
                f'<label><input type="checkbox" name="firewall" value="1"{" checked" if n.firewall else ""}> Настроить файрвол ufw (открыть SSH и порт Xray)</label></div>'
                '<div><button class="btn" type="submit">Сохранить параметры</button></div>'
                '<p class="hint">Mux работает только без flow. Смена порта или SNI меняет ссылки клиентов: после «Применить» клиентам нужно обновить подписку в Happ.</p>',
            )
            + "</details>"
        )
    edit_html = "".join(edit_blocks) or '<p class="muted">серверов нет</p>'
    direct_html = "".join(f'<label><input type="checkbox" name="nodes" value="{esc(n.name)}"> {esc(n.name)} · {esc(n.host)} {esc(n.country)}</label>' for n in topo.nodes.values()) or '<span class="muted">серверов нет</span>'
    direct = form(
        ctx, "direct_create",
        f'<div class="checks">{direct_html}</div><div><button class="btn" type="submit">Создать прямой доступ</button></div>'
        '<p class="hint">Клиент подключается к одному серверу и выходит в интернет с его адреса, без второго сервера. Все параметры сервера (порт, SNI, flow, mux, торренты) и маршрутизация Happ работают так же.</p>',
    )
    ver = data.latest_verify(ctx.db)
    chain_rows = []
    for k, ch in topo.chains.items():
        v = ver.get(k)
        chain_rows.append([
            f"<strong>{esc(k)}</strong>", esc(ch.title), esc(" → ".join(ch.path)),
            badge("проверен " + fmt_ago(v[1]), "good") if v and v[0] else badge("не прошёл", "bad") if v else badge("не проверен"),
            esc(sum(1 for c in clients if k in c.chains)),
            button_form(ctx, "chain_delete", "Удалить", {"name": k}, cls="btn danger sm", confirm="Точно? Нажмите ещё раз"),
        ])
    chains_html = table(["Имя", "Название", "Путь", "Проверка", "Клиентов", ""], chain_rows, num={4}, empty="каскадов нет")
    ops = (
        button_form(ctx, "apply", "Применить на серверах", cls="btn")
        + " " + button_form(ctx, "verify", "Проверить каскады")
        + " " + button_form(ctx, "refresh", "Обновить состояние")
        + '<p class="hint">«Применить» ставит и настраивает Xray на всех серверах и выпускает ссылки. Повторный запуск безопасен.</p>'
    )
    jobs = ctx.db.q("SELECT id,title,status,started,finished FROM jobs ORDER BY id DESC LIMIT 8")
    job_rows = [[f'<a href="{url("job", id=j["id"])}">{esc(j["title"])}</a>', badge(STATUS_LABEL.get(j["status"], j["status"]), {"ok": "good", "error": "bad"}.get(j["status"], "warn")), esc(fmt_dt(j["started"]))] for j in jobs]
    body = (
        f"<h1>Серверы и каскады</h1>{pending_banner(ctx)}"
        '<div class="stack" data-sortable="servers">'
        + block("add", "Добавить сервер", add) + block("list", "Серверы", srv_table) + block("params", "Порты и параметры серверов", edit_html)
        + block("cascade", "Создать каскады", cascade) + block("direct", "Прямой доступ (без каскада)", direct) + block("chains", "Каскады", chains_html)
        + block("ops", "Установка и проверка", ops) + block("jobs", "Последние операции", table(["Операция", "Итог", "Начало"], job_rows, empty="операций ещё не было"))
        + '</div><p><button class="btn ghost sm" type="button" data-dnd-reset>Сбросить порядок блоков</button></p>'
    )
    return render(ctx, "servers", "Серверы и каскады", body)


def job_json(ctx) -> str:
    j = ctx.db.one("SELECT * FROM jobs WHERE id=?", (int(ctx.q.get("id", "0") or 0),))
    if not j:
        raise NotFound()
    return json.dumps({"status": j["status"], "status_label": STATUS_LABEL.get(j["status"], j["status"]), "log": j["log"]}, ensure_ascii=False)


def job_page(ctx) -> str:
    ident = ctx.q.get("id", "")
    j = ctx.db.one("SELECT * FROM jobs WHERE id=?", (int(ident) if ident.isdigit() else 0,))
    if not j:
        raise NotFound()
    running = "1" if j["status"] == "running" else "0"
    kind = {"ok": "good", "error": "bad"}.get(j["status"], "warn")
    body = (
        f'<p><a href="{url("servers")}">← серверы и каскады</a></p><h1>{esc(j["title"])}</h1>'
        f'<p>Итог: <span id="jobstatus">{badge(STATUS_LABEL.get(j["status"], j["status"]), kind)}</span> · начато {esc(fmt_dt(j["started"]))}'
        f'{" · закончено " + esc(fmt_dt(j["finished"])) if j["finished"] else ""}</p>'
        f'<pre class="pre" id="joblog" data-id="{j["id"]}" data-running="{running}">{esc(j["log"])}</pre>'
    )
    return render(ctx, "job", "Операция", body)


# ---------------------------------------------------------------- система

def system_page(ctx) -> str:
    topo, clients, state = _state(ctx)
    interval = ctx.app.collector.interval()
    label, kind = data.collector_state(ctx.db, interval)
    last_end = int(ctx.db.kv_get("collector.last_end", "0") or 0)
    err = ctx.db.kv_get("collector.last_error")
    coll = (
        f'<dl class="kv"><dt>Состояние</dt><dd>{badge(label, kind)}</dd><dt>Последний проход</dt><dd>{esc(fmt_dt(last_end))} ({esc(fmt_ago(last_end))})</dd>'
        f'<dt>Интервал</dt><dd>{interval // 60} мин (в настройках)</dd><dt>Последняя ошибка</dt><dd>{esc(err or "нет")}</dd></dl>'
        + button_form(ctx, "refresh", "Обновить сейчас")
    )
    rows = []
    for name, n in topo.nodes.items():
        s = data.latest_sample(ctx.db, name)
        st, k = data.server_state(s, interval=interval)
        if s and s["ok"]:
            rows.append([esc(name), badge(st, k), esc(s["xray"]), esc(f'{s["mem_mb"]} МБ' if s["mem_mb"] is not None else "-"), esc(f'{s["swap_mb"]} МБ' if s["swap_mb"] is not None else "-"), esc(s["load"]), esc(f'{s["disk_pct"]}%' if s["disk_pct"] is not None else "-"), esc(fmt_ago(s["ts"]))])
        else:
            rows.append([esc(name), badge(st, k), "-", "-", "-", "-", "-", esc(fmt_ago(s["ts"]) if s else "нет данных")])
    servers = table(["Сервер", "Состояние", "Xray", "Свободно RAM", "Swap", "Load", "Диск", "Замер"], rows, empty="серверов нет")
    pub = ""
    pubfile = Path.home() / ".ssh" / "id_ed25519.pub"
    if pubfile.exists():
        pub = pubfile.read_text().strip()
    du = shutil.disk_usage(ctx.ws.root)
    dbsize = ctx.db.path.stat().st_size if ctx.db.path.exists() else 0
    host = (
        f'<dl class="kv"><dt>Версия lerk-vpn</dt><dd>{esc(__version__)} <span class="mono">{esc(ctx.app.updater.current()["commit"])}</span> {badge("есть обновление", "warn") if ctx.db.kv_get("update.ahead") not in ("", "0") else ""}</dd><dt>Python</dt><dd>{esc(platform.python_version())}</dd>'
        f'<dt>Рабочая папка</dt><dd class="mono">{esc(ctx.ws.root)}</dd><dt>Диск</dt><dd>свободно {esc(fmt_bytes(du.free))} из {esc(fmt_bytes(du.total))}</dd>'
        f'<dt>База панели</dt><dd>{esc(fmt_bytes(dbsize))}</dd><dt>Вход по паролю</dt><dd>{badge("включён", "good") if ctx.app.auth.enabled else badge("выключен", "bad")}</dd>'
        f'<dt>SSH-ключ панели</dt><dd>{badge("есть", "good") if pub else badge("нет", "bad")}</dd></dl>'
        + (f'<h3>Публичный ключ панели (для ручного добавления на сервер)</h3><div class="pre">{esc(pub)}</div>' if pub else "")
    )
    logs = _tail(ctx.ws.root / "panel.log", 60, ctx.q.get("lf", ""))
    log_form = f'<form class="form" method="get" action="/"><input type="hidden" name="p" value="system"><div class="row"><label>Фильтр лога<input type="search" name="lf" value="{esc(ctx.q.get("lf", ""))}"></label><button class="btn ghost" type="submit">Показать</button></div></form>'
    jobs = ctx.db.q("SELECT id,title,status,started,finished FROM jobs ORDER BY id DESC LIMIT 15")
    job_rows = [[f'<a href="{url("job", id=j["id"])}">{esc(j["title"])}</a>', badge(STATUS_LABEL.get(j["status"], j["status"]), {"ok": "good", "error": "bad"}.get(j["status"], "warn")), esc(fmt_dt(j["started"])), esc(f'{j["finished"] - j["started"]} с' if j["finished"] else "-")] for j in jobs]
    body = (
        f"<h1>Система</h1>{pending_banner(ctx)}"
        '<div class="cols" data-sortable="system">'
        + block("collector", "Сбор данных", coll) + block("host", "Панель", host)
        + block("servers", "Серверы", servers) + block("jobs", "Операции", table(["Операция", "Итог", "Начало", "Длилась"], job_rows, empty="операций ещё не было"))
        + block("log", "Журнал панели", log_form + f'<pre class="pre">{esc(logs or "пусто")}</pre>')
        + '</div><p><button class="btn ghost sm" type="button" data-dnd-reset>Сбросить порядок блоков</button></p>'
    )
    return render(ctx, "system", "Система", body)


def _tail(path: Path, n: int, flt: str) -> str:
    if not path.exists():
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64_000))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    if flt:
        lines = [ln for ln in lines if flt.lower() in ln.lower()]
    return "\n".join(lines[-n:])


# ---------------------------------------------------------------- настройки, журнал, поиск

def settings_page(ctx) -> str:
    cur = ctx.settings.all()
    groups_html = []
    for gkey, glabel in GROUPS.items():
        if gkey == "routing":
            continue
        fields_html = []
        for f in [x for x in FIELDS if x.group == gkey]:
            v = cur[f.key]
            changed = v != f.default
            cls = ' class="changed"' if changed else ""
            was = f'<span class="hint">по умолчанию: {esc(("вкл" if f.default else "выкл") if f.kind == "bool" else f.default)}</span>' if changed else ""
            if f.kind == "bool":
                inp = f'<input type="checkbox" name="{esc(f.key)}" value="1"{" checked" if v else ""}>'
                fields_html.append(f"<label{cls}><span>{inp} {esc(f.label)}</span>{was}</label>")
            else:
                typ = "number" if f.kind == "int" else "text"
                attrs = f' min="{f.lo}" max="{f.hi}"' if f.kind == "int" else f' maxlength="{f.hi}"'
                hint = f'<span class="hint">{esc(f.hint)}</span>' if f.hint else ""
                fields_html.append(f'<label{cls}>{esc(f.label)}<input type="{typ}" name="{esc(f.key)}" value="{esc(v)}"{attrs}>{hint}{was}</label>')
        groups_html.append(block(gkey, glabel, '<div class="fields">' + "".join(fields_html) + "</div>"))
    updates = updates_block(ctx)
    body_form = form(ctx, "settings_save", f'<input type="hidden" name="_scope" value="{",".join(g for g in GROUPS if g != "routing")}"><div class="cols" data-sortable="settings">{"".join(groups_html)}</div><div><button class="btn" type="submit">Сохранить настройки</button></div>')
    log = ctx.db.q("SELECT ts,key,old,new,ip FROM settings_log ORDER BY id DESC LIMIT 30")
    log_rows = [[esc(fmt_dt(r["ts"])), esc(BY_KEY[r["key"]].label if r["key"] in BY_KEY else r["key"]), esc(r["old"]), esc(r["new"]), esc(r["ip"] or "")] for r in log]
    body = (
        f"<h1>Настройки</h1><p class=\"muted\">Действуют сразу, без правки кода и перезапуска. Изменённые поля подсвечены.</p>"
        + '<div class="stack" data-sortable="upd">' + block("updates", "Обновление панели", updates) + "</div>" + body_form
        + '<div class="stack">' + block("log", "Журнал изменений", table(["Когда", "Настройка", "Было", "Стало", "Адрес"], log_rows, empty="изменений не было")) + "</div>"
        '<p><button class="btn ghost sm" type="button" data-dnd-reset>Сбросить порядок блоков</button></p>'
    )
    return render(ctx, "settings", "Настройки", body)


def updates_block(ctx) -> str:
    u = ctx.app.updater
    cur, st = u.current(), u.state()
    if not u.available:
        return '<p class="muted">Панель установлена без git, обновление кнопкой недоступно. Используйте установочную команду из README.</p>'
    ahead = st["ahead"]
    if st["error"]:
        status = badge("не удалось проверить", "bad") + f' <span class="muted">{esc(st["error"])}</span>'
    elif not st["checked"]:
        status = badge("ещё не проверяли")
    elif ahead and ahead != "0":
        status = badge(f"доступно обновлений: {ahead}", "warn")
    else:
        status = badge("установлена последняя версия", "good")
    commits = "".join(f"<li>{esc(c)}</li>" for c in st["commits"])
    info = (
        f'<dl class="kv"><dt>Версия</dt><dd>{esc(cur["version"])} <span class="mono">{esc(cur["commit"])}</span> {esc(cur["subject"])}</dd>'
        f'<dt>Проверено</dt><dd>{esc(fmt_dt(st["checked"]) if st["checked"] else "нет данных")}</dd><dt>Состояние</dt><dd>{status}</dd></dl>'
        + (f'<h3>Что нового</h3><ul>{commits}</ul>' if commits else "")
    )
    buttons = button_form(ctx, "update_check", "Проверить обновления", cls="btn")
    buttons += " " + button_form(ctx, "update_install", "Установить обновления", cls="btn ghost", confirm="Панель перезапустится на несколько секунд. Нажмите ещё раз")
    if st["prev"]:
        buttons += " " + button_form(ctx, "update_rollback", "Откатить на предыдущую версию", cls="btn ghost sm", confirm="Откатить? Нажмите ещё раз")
    return info + f"<p>{buttons}</p>" + '<p class="hint">Установка делает git pull, проверяет, что новая версия запускается и тесты проходят, затем перезапускает панель. Если проверка не пройдена, версия откатывается сама.</p>'


def routing_page(ctx) -> str:
    cur = ctx.settings.all()
    geo = ctx.app.geo
    status = geo.status()
    st_rows = []
    for kind, label in (("geoip", "geoip.dat (адреса по странам)"), ("geosite", "geosite.dat (сайты по тегам)")):
        x = status[kind]
        st_rows.append([esc(label), badge("есть", "good") if x["exists"] else badge("не скачан", "bad"), esc(fmt_bytes(x["size"]) if x["exists"] else "-"), esc(x["tags"] or "-"), esc(fmt_dt(x["mtime"]) if x["exists"] else "-")])
    prof = happ.routing_profile(cur, int(ctx.db.kv_get("happ.routing_updated", "0") or 0) or None, geo)

    def chk(key: str) -> str:
        f = BY_KEY[key]
        hint = f'<br><span class="hint">{esc(f.hint)}</span>' if f.hint else ""
        return f'<label><input type="checkbox" name="{key}" value="1"{" checked" if cur[key] else ""}><span>{esc(f.label)}{hint}</span></label>'

    presets = form(
        ctx, "settings_save",
        '<input type="hidden" name="_scope" value="routing">'
        f'<div class="checks stackv">{chk("happ.direct_ru")}{chk("happ.geo_ru")}{chk("happ.ru_services")}{chk("happ.block_ads")}{chk("happ.geo_auto")}</div>'
        f'<p class="hint">Сейчас напрямую идёт {len(prof["DirectSites"])} правил по сайтам и {len(prof["DirectIp"])} по адресам. Остальное идёт через VPN. Изменения попадают к клиентам при следующем обновлении подписки в Happ.</p>'
        '<div><button class="btn" type="submit">Сохранить</button></div>',
    )
    lists = ""
    for key in ("happ.direct_sites", "happ.direct_ips", "happ.proxy_sites", "happ.proxy_ips", "happ.block_sites", "happ.block_ips"):
        f = BY_KEY[key]
        lists += f'<label>{esc(f.label)}<textarea name="{key}" rows="4" spellcheck="false">{esc(cur[key])}</textarea><span class="hint">{esc(f.hint)}</span></label>'
    lists_form = form(ctx, "settings_save", f'<div class="cols">{lists}</div><div><button class="btn" type="submit">Сохранить правила</button></div>')
    urls_form = form(
        ctx, "settings_save",
        f'<label>{esc(BY_KEY["happ.geoip_url"].label)}<input type="text" name="happ.geoip_url" value="{esc(cur["happ.geoip_url"])}"></label>'
        f'<label>{esc(BY_KEY["happ.geosite_url"].label)}<input type="text" name="happ.geosite_url" value="{esc(cur["happ.geosite_url"])}"></label>'
        '<div><button class="btn ghost" type="submit">Сохранить адреса</button></div>',
    )
    geo_html = table(["Файл", "Состояние", "Размер", "Тегов", "Обновлён"], st_rows) + f"<p>{button_form(ctx, 'geo_update', 'Обновить geo-базы', cls='btn')}</p>" + urls_form
    if not geo.loaded():
        geo_html = '<div class="flash info">Geo-базы ещё не скачаны. Нажмите «Обновить geo-базы»: без них панель не может проверять теги и искать их в каталоге.</div>' + geo_html

    # каталог тегов
    q, kind = ctx.q.get("tq", "").strip(), ctx.q.get("tk", "geosite")
    kind = kind if kind in ("geosite", "geoip") else "geosite"
    found = geo.search(kind, q) if (q and geo.loaded()) else []
    key_by_kind = {"geosite": ("happ.direct_sites", "happ.proxy_sites", "happ.block_sites"), "geoip": ("happ.direct_ips", "happ.proxy_ips", "happ.block_ips")}[kind]
    labels = ("напрямую", "через VPN", "блок")
    cat_rows = []
    for tag, n in found:
        token = f"{kind}:{tag}"
        btns = " ".join(button_form(ctx, "route_add", lab, {"list": k, "token": token}, cls="btn ghost sm") for k, lab in zip(key_by_kind, labels))
        cat_rows.append([f'<span class="mono">{esc(token)}</span>', esc(n), btns])
    kinds = "".join(f'<option value="{v}"{" selected" if v == kind else ""}>{l}</option>' for v, l in (("geosite", "сайты (geosite)"), ("geoip", "страны и сети (geoip)")))
    catalog = (
        f'<form class="form" method="get" action="/"><input type="hidden" name="p" value="routing"><div class="row"><label>Тег или часть названия<input type="search" name="tq" value="{esc(q)}" placeholder="например yandex, ru, telegram"></label>'
        f'<label>Вид<select name="tk">{kinds}</select></label><button class="btn ghost" type="submit">Найти</button></div></form>'
        + (table(["Тег", "Записей", "Добавить в список"], cat_rows, num={1}, empty="тегов не найдено") if q else '<p class="hint">Введите часть названия. Например «yandex», «vk», «ru» или «telegram».</p>')
    )
    # проверка адреса
    probe = ctx.q.get("chk", "").strip()
    result = ""
    if probe:
        result = check_target(probe, prof, geo)
    check = (
        f'<form class="form" method="get" action="/"><input type="hidden" name="p" value="routing"><div class="row"><label>Домен или IP-адрес<input type="text" name="chk" value="{esc(probe)}" placeholder="yandex.com или 77.88.8.8"></label>'
        f'<button class="btn ghost" type="submit">Проверить</button></div></form>{result}'
    )
    profile_html = (
        f'<p><a class="btn ghost" href="{esc(happ.routing_link(prof))}">Добавить профиль в Happ на этом устройстве</a></p>'
        f'<pre class="pre">{esc(json.dumps(prof, ensure_ascii=False, indent=2))}</pre>'
    )
    body = (
        f"<h1>Маршрутизация</h1><p class=\"muted\">Что идёт напрямую, а что через VPN. Правило работает на устройстве клиента в Happ: российский трафик не уходит в VPN.</p>"
        '<div class="stack" data-sortable="routing">'
        + block("presets", "Россия напрямую", presets) + block("lists", "Свои правила", lists_form)
        + block("catalog", "Каталог тегов", catalog) + block("check", "Проверка адреса", check)
        + block("geo", "Geo-базы", geo_html) + block("profile", "Итоговый профиль для Happ", profile_html) + "</div>"
        '<p><button class="btn ghost sm" type="button" data-dnd-reset>Сбросить порядок блоков</button></p>'
    )
    return render(ctx, "routing", "Маршрутизация", body)


def check_target(target: str, prof: dict, geo) -> str:
    """Какое правило профиля сработает для домена или IP и куда пойдёт трафик."""
    import ipaddress

    try:
        ip = ipaddress.ip_address(target)
    except ValueError:
        ip = None
    if ip is None and not geo.loaded():
        note = "geo-базы не скачаны, теги geosite не проверяются"
    else:
        note = ""
    order = [("block", "блокируется", prof["BlockSites"], prof["BlockIp"]), ("direct", "напрямую", prof["DirectSites"], prof["DirectIp"]), ("proxy", "через VPN", prof["ProxySites"], prof["ProxyIp"])]
    for kind, label, sites, ips in order:
        for rule in (ips if ip else sites):
            hit = geomod.ip_matches(ip, rule, geo) if ip else geomod.domain_matches(target, rule, geo)
            if hit:
                cls = {"block": "bad", "direct": "good", "proxy": "warn"}[kind]
                return f'<div class="flash {"ok" if kind == "direct" else "info"}"><strong class="{cls}">{esc(target)}: {label}</strong>, сработало правило <span class="mono">{esc(hit)}</span></div>'
    return f'<div class="flash info"><strong>{esc(target)}: через VPN</strong>, ни одно правило «напрямую» не подошло (по умолчанию весь остальной трафик идёт через VPN). {esc(note)}</div>'


def audit_page(ctx) -> str:
    q = ctx.q.get("q", "").strip()
    per_page = 40
    pg = int(ctx.q["pg"]) if ctx.q.get("pg", "").isdigit() and int(ctx.q["pg"]) > 0 else 1
    if q:
        like = f"%{q}%"
        total = ctx.db.one("SELECT COUNT(*) AS n FROM audit WHERE action LIKE ? OR target LIKE ? OR detail LIKE ?", (like, like, like))["n"]
        rows = ctx.db.q("SELECT * FROM audit WHERE action LIKE ? OR target LIKE ? OR detail LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?", (like, like, like, per_page, (pg - 1) * per_page))
    else:
        total = ctx.db.one("SELECT COUNT(*) AS n FROM audit")["n"]
        rows = ctx.db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ? OFFSET ?", (per_page, (pg - 1) * per_page))
    trs = []
    for r in rows:
        target = r["target"] or ""
        link = f'<a href="{url("client", name=target)}">{esc(target)}</a>' if r["action"].startswith("клиент") and target else esc(target)
        trs.append([esc(fmt_dt(r["ts"])), esc(r["actor"]), esc(r["action"]), link, esc(r["detail"] or ""), esc(r["ip"] or "")])
    flt = f'<form class="form" method="get" action="/"><input type="hidden" name="p" value="audit"><div class="row"><label>Поиск по журналу<input type="search" name="q" value="{esc(q)}"></label><button class="btn ghost" type="submit">Найти</button></div></form>'
    body = (
        "<h1>Журнал действий</h1>"
        + '<div class="stack" data-sortable="audit">' + block("log", f"Записи ({total})", flt + table(["Когда", "Кто", "Действие", "Над чем", "Подробности", "Адрес"], trs, empty="записей нет") + pager("audit", total, per_page, pg, q=q)) + "</div>"
        '<p><button class="btn ghost sm" type="button" data-dnd-reset>Сбросить порядок блоков</button></p>'
    )
    return render(ctx, "audit", "Журнал", body)


def search_page(ctx) -> str:
    topo, clients, state = _state(ctx)
    q = ctx.q.get("q", "").strip().lower()
    out = []
    if q:
        for c in clients:
            uuids = " ".join(state.data["clients"].get(c.name, {}).get("uuids", {}).values())
            if q in c.name.lower() or q in c.note.lower() or q in uuids or q in state.token(c.name):
                out.append(["клиент", f'<a href="{url("client", name=c.name)}">{esc(c.name)}</a>', esc(", ".join(c.chains))])
        for n in topo.nodes.values():
            if q in n.name.lower() or q in n.host.lower() or q in n.country.lower():
                out.append(["сервер", f'<a href="{url("servers")}">{esc(n.name)}</a>', esc(f"{n.host} {n.country}")])
        for ch in topo.chains.values():
            if q in ch.name.lower() or q in ch.title.lower():
                out.append(["каскад", f'<a href="{url("servers")}">{esc(ch.name)}</a>', esc(" → ".join(ch.path))])
    body = f"<h1>Поиск: {esc(q)}</h1>" + block("res", "Найдено", table(["Что", "Название", "Подробности"], out, empty="ничего не найдено" if q else "введите запрос"))
    return render(ctx, "search", "Поиск", body)


PAGES = {
    "pulse": pulse, "happ": happ_page, "routing": routing_page, "qr": qr_page, "clients": clients_page, "client": client_page, "servers": servers_page, "job": job_page,
    "system": system_page, "settings": settings_page, "audit": audit_page, "search": search_page,
}
