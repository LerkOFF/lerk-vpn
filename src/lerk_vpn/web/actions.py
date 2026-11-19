"""Действия панели (POST /do/<имя>). Каждое пишется в журнал; опасные подтверждаются в браузере вторым нажатием."""

from __future__ import annotations

import re
import threading

from .. import ops
from ..model import SpecError
from ..remote import RemoteError
from .ui import url
from .updater import restart_soon

Result = tuple[str, str, str]  # (ok|err|info, текст, куда вернуться)


def _dirty(ctx) -> None:
    ctx.db.kv_set("dirty", "1")


def client_add(ctx) -> Result:
    name = ctx.form.get("name", "").strip().lower()
    ops.client_add(ctx.ws, name, ctx.forms.get("chains", []), ctx.form.get("note", ""))
    ctx.audit("клиент создан", name, ", ".join(ctx.forms.get("chains", [])))
    _dirty(ctx)
    return "ok", f"Клиент {name} создан. Нажмите «Применить на серверах», чтобы он заработал.", url("client", name=name)


def client_toggle(ctx) -> Result:
    name = ctx.form.get("name", "")
    topo, clients, _ = ctx.ws.load()
    current = next((c for c in clients if c.name == name), None)
    if not current:
        raise SpecError("клиента нет")
    ops.client_update(ctx.ws, name, enabled=not current.enabled)
    ctx.audit("клиент выключен" if current.enabled else "клиент включён", name)
    _dirty(ctx)
    return "ok", f"Клиент {name} {'выключен' if current.enabled else 'включён'}. Примените изменения на серверах.", url("client", name=name)


def client_note(ctx) -> Result:
    name = ctx.form.get("name", "")
    ops.client_update(ctx.ws, name, note=ctx.form.get("note", ""))
    ctx.audit("клиент заметка", name)
    return "ok", "Заметка сохранена.", url("client", name=name)


def client_chains(ctx) -> Result:
    name = ctx.form.get("name", "")
    ops.client_update(ctx.ws, name, chains=ctx.forms.get("chains", []))
    ctx.audit("клиент каскады", name, ", ".join(ctx.forms.get("chains", [])))
    _dirty(ctx)
    return "ok", "Каскады клиента изменены. Примените изменения на серверах.", url("client", name=name)


def client_rotate(ctx) -> Result:
    name = ctx.form.get("name", "")
    ops.client_rotate(ctx.ws, name)
    ctx.audit("клиент новые ключи", name)
    _dirty(ctx)
    return "ok", "Выпущены новые ключи и адрес подписки. Старые перестанут работать после применения.", url("client", name=name)


def client_delete(ctx) -> Result:
    name = ctx.form.get("name", "")
    ops.client_delete(ctx.ws, name)
    ctx.audit("клиент удалён", name)
    _dirty(ctx)
    return "ok", f"Клиент {name} удалён. Примените изменения на серверах.", url("clients")


def server_role(ctx) -> Result:
    ops.server_set_role(ctx.ws, ctx.form.get("name", ""), ctx.form.get("role", ""))
    ctx.audit("сервер роль", ctx.form.get("name", ""), ctx.form.get("role", ""))
    return "ok", "Роль сохранена.", url("servers")


def server_edit(ctx) -> Result:
    name = ctx.form.get("name", "")
    changed = ops.server_edit(ctx.ws, name, ctx.form)
    if not changed:
        return "info", "Изменений нет.", url("servers")
    _dirty(ctx)
    ctx.audit("сервер параметры", name, ", ".join(changed))
    return "ok", f"Сохранено: {', '.join(changed)}. Нажмите «Применить», чтобы изменения попали на серверы; ссылки клиентов обновятся сами при следующем обновлении подписки.", url("servers")


def server_delete(ctx) -> Result:
    name = ctx.form.get("name", "")
    ops.server_delete(ctx.ws, name)
    ctx.audit("сервер удалён из панели", name)
    return "ok", f"Сервер {name} убран из панели. Сам сервер не тронут, Xray на нём остался.", url("servers")


def cascade_create(ctx) -> Result:
    created = ops.cascade_create(ctx.ws, ctx.forms.get("entries", []), ctx.form.get("exit", ""))
    ctx.audit("каскады созданы", ", ".join(created))
    _dirty(ctx)
    return "ok", f"Создано каскадов: {len(created)} ({', '.join(created)}). Добавьте клиентов и примените изменения.", url("servers")


def chain_delete(ctx) -> Result:
    name = ctx.form.get("name", "")
    ops.chain_delete(ctx.ws, name)
    ctx.audit("каскад удалён", name)
    _dirty(ctx)
    return "ok", f"Каскад {name} удалён. Клиенты без каскадов отключены. Примените изменения.", url("servers")


def _store_verify(ctx, outcomes) -> None:
    import time

    now = int(time.time())
    for chain, ok, ip in outcomes:
        ctx.db.x("INSERT INTO verify(ts,chain,ok,ip) VALUES(?,?,?,?)", (now, chain, 1 if ok else 0, ip))


def apply_job(ctx) -> Result:
    ws, db = ctx.ws, ctx.db

    def run(log):
        code, _ = ops.run_apply(ws, None, log=log)
        if code:
            return code
        db.kv_set("dirty", "0")
        log("Проверяю каскады сквозным запросом...")
        vcode, outcomes = ops.run_verify(ws, None, log=log)
        _store_verify(ctx, outcomes)
        return vcode

    jid = ctx.app.jobs.start("apply", "Применение на серверах", ctx.actor, run)
    if jid is None:
        return "err", "Другая операция ещё выполняется, подождите.", url("servers")
    ctx.audit("применение запущено", "", f"операция {jid}")
    return "info", "Применение запущено, журнал ниже.", url("job", id=jid)


def verify_job(ctx) -> Result:
    def run(log):
        code, outcomes = ops.run_verify(ctx.ws, None, log=log)
        _store_verify(ctx, outcomes)
        return code

    jid = ctx.app.jobs.start("verify", "Проверка каскадов", ctx.actor, run)
    if jid is None:
        return "err", "Другая операция ещё выполняется, подождите.", url("servers")
    ctx.audit("проверка запущена", "", f"операция {jid}")
    return "info", "Проверка запущена.", url("job", id=jid)


def refresh(ctx) -> Result:
    threading.Thread(target=ctx.app.collector.run_once, daemon=True, name="refresh").start()
    ctx.audit("обновление состояния", "")
    return "info", "Опрашиваю серверы, обновите страницу через несколько секунд.", ctx.referer_page("servers")


def server_add(ctx) -> Result:
    host = ctx.form.get("host", "").strip()
    user = ctx.form.get("user", "root").strip() or "root"
    password = ctx.form.get("password", "")
    role = ctx.form.get("role", "entry")
    install = bool(ctx.form.get("install"))
    port = 22
    if not host:
        raise SpecError("укажите IP-адрес сервера")
    s = ctx.settings.all()
    pub = ctx.app.pubkey
    ws = ctx.ws

    def run(log):
        name = ops.server_add(ws, host, user, password, role, pub, port, log=log, sni=s["new.default_sni"], swap_gb=s["new.default_swap_gb"])
        if install:
            log(f"Устанавливаю и настраиваю Xray на {name}...")
            code, _ = ops.run_apply(ws, [name], log=log)
            if code:
                return code
        else:
            log("Установка Xray пропущена, запустите «Применить на серверах» позже.")
        return 0

    jid = ctx.app.jobs.start("server_add", f"Добавление сервера {host}", ctx.actor, run)
    if jid is None:
        return "err", "Другая операция ещё выполняется, подождите.", url("servers")
    ctx.audit("сервер добавляется", host, f"логин {user}, роль {role}")  # пароль в журнал не пишется
    return "info", "Добавляю сервер, журнал ниже.", url("job", id=jid)


def settings_save(ctx) -> Result:
    back = ctx.referer_page("settings")
    changed, errors = ctx.settings.save(ctx.form, ctx.ip)
    if changed:
        ctx.audit("настройки", ", ".join(changed))
    if errors:
        return "err", " ".join(errors), back
    return "ok", ("Сохранено: " + ", ".join(changed)) if changed else "Изменений нет.", back


def route_add(ctx) -> Result:
    from .happ import split_list
    from .settings import LIST_KEYS

    key, token = ctx.form.get("list", ""), ctx.form.get("token", "").strip()
    if key not in LIST_KEYS:
        raise ValueError("неизвестный список")
    items = split_list(ctx.settings.get(key))
    if token in items:
        return "info", f"{token} уже в списке.", url("routing", tq=ctx.form.get("tq", ""))
    changed, errors = ctx.settings.save({key: "\n".join(items + [token])}, ctx.ip)
    if errors:
        return "err", " ".join(errors), url("routing")
    ctx.audit("маршрутизация добавлено", token, key)
    return "ok", f"{token} добавлено.", ctx.referer_page("routing")


def geo_update(ctx) -> Result:
    def run(log):
        ok = ctx.app.geo.download(ctx.app.geo_urls(), log=log)
        if ok:
            ctx.db.kv_set("happ.routing_updated", str(int(__import__("time").time())))
        return 0 if ok else 1

    jid = ctx.app.jobs.start("geo", "Обновление geo-баз", ctx.actor, run)
    if jid is None:
        return "err", "Другая операция ещё выполняется, подождите.", url("routing")
    ctx.audit("geo-базы обновление запущено", "")
    return "info", "Скачиваю geo-базы.", url("job", id=jid)


def update_check(ctx) -> Result:
    res = ctx.app.updater.check()
    ctx.audit("проверка обновлений", "", f"новых коммитов: {res['ahead']}" if res["ok"] else res["error"])
    if not res["ok"]:
        return "err", "Не удалось проверить: " + res["error"], url("settings")
    return "ok", (f"Доступно обновлений: {res['ahead']}." if res["ahead"] else "Установлена последняя версия."), url("settings")


def update_install(ctx) -> Result:
    ok, msg = ctx.app.updater.install()
    ctx.audit("установка обновления", "", msg)
    if not ok:
        return "err", msg, url("settings")
    if msg.startswith("уже"):
        return "ok", msg, url("settings")
    restart_soon()
    return "ok", msg + ". Панель перезапускается, страница обновится сама.", url("settings", restarting=1)


def update_rollback(ctx) -> Result:
    ok, msg = ctx.app.updater.rollback()
    ctx.audit("откат обновления", "", msg)
    if not ok:
        return "err", msg, url("settings")
    restart_soon()
    return "ok", msg + ". Панель перезапускается.", url("settings", restarting=1)


ACTIONS = {
    "client_add": client_add, "client_toggle": client_toggle, "client_note": client_note, "client_chains": client_chains,
    "client_rotate": client_rotate, "client_delete": client_delete, "server_role": server_role, "server_edit": server_edit, "server_delete": server_delete,
    "cascade_create": cascade_create, "chain_delete": chain_delete, "apply": apply_job, "verify": verify_job, "refresh": refresh,
    "server_add": server_add, "settings_save": settings_save, "update_check": update_check, "update_install": update_install,
    "update_rollback": update_rollback, "route_add": route_add, "geo_update": geo_update,
}
