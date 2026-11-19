"""Командная строка lerk-vpn."""

from __future__ import annotations

import argparse
import base64
import getpass
import os
import subprocess
import sys
from pathlib import Path

from . import __version__, ops
from .model import Client, SpecError, Topology, load_clients, load_topology, save_clients
from .provision import ApplyResult, apply_node, config_digest, node_status, render, verify_chain
from .remote import Remote, RemoteError, install_key
from .serve import serve
from .state import State
from .workspace import Workspace, pick_nodes, write_exports
from .templates import GITIGNORE, TOPOLOGY
from .xraycfg import apply_order, build_node_config, chains_through, client_links, users_at

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def ok(text: str) -> str:
    return paint(text, "32")


def bad(text: str) -> str:
    return paint(text, "31")


def warn(text: str) -> str:
    return paint(text, "33")


def table(rows: list[list[str]], header: list[str]) -> str:
    cols = [header] + rows
    widths = [max(len(_plain(r[i])) for r in cols) for i in range(len(header))]
    lines = []
    for n, row in enumerate(cols):
        lines.append("  ".join(_plain_pad(cell, widths[i]) for i, cell in enumerate(row)))
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _plain(s: str) -> str:
    import re

    return re.sub(r"\033\[[0-9;]*m", "", s)


def _plain_pad(s: str, width: int) -> str:
    return s + " " * (width - len(_plain(s)))


# ---------------------------------------------------------------- команды

def cmd_init(args, ws: Workspace) -> int:
    ws.root.mkdir(parents=True, exist_ok=True)
    created = []
    for path, content in ((ws.topology_path, TOPOLOGY), (ws.clients_path, '{\n  "clients": []\n}\n'), (ws.root / ".gitignore", GITIGNORE)):
        if path.exists():
            print(f"уже есть: {path.name}")
            continue
        path.write_text(content, "utf-8")
        created.append(path.name)
    if created:
        print("создано: " + ", ".join(created))
    print(f"дальше: отредактируйте {ws.topology_path}, затем lerk-vpn plan")
    return 0


def cmd_plan(args, ws: Workspace) -> int:
    topo, clients, state = ws.load()
    state.ensure(topo, clients)  # только в памяти
    if args.show_config:
        if args.show_config not in topo.nodes:
            raise SpecError(f"сервера '{args.show_config}' нет")
        print(render(build_node_config(topo, clients, state, args.show_config)), end="")
        return 0
    print(f"проект: {topo.project}   Xray: {topo.xray_version or 'последняя'}")
    print("\nЦепочки:")
    for chain in topo.chains.values():
        n_clients = sum(1 for c in clients if c.enabled and chain.name in c.chains)
        print(f"  {chain.name}: {' -> '.join(chain.path)} -> интернет   клиентов: {n_clients}")
    rows = []
    for name in apply_order(topo):
        node = topo.nodes[name]
        through = chains_through(topo, name)
        roles = []
        for ch in through:
            pos = ch.path.index(name)
            roles.append(f"{ch.name}:{'вход' if pos == 0 else 'выход' if pos == len(ch.path) - 1 else 'звено'}" if len(ch.path) > 1 else f"{ch.name}:один")
        if len(users_at(topo, clients, state, name)) == 0:
            roles.append("без пользователей")
        rows.append([name, node.country or "-", node.host, str(node.port), node.sni, f"{node.swap_gb} ГБ", ", ".join(roles) or "не в цепочке"])
    print("\nСерверы (в порядке установки):")
    print(table(rows, ["сервер", "страна", "адрес", "порт", "sni", "swap", "роли"]))
    unused = [n for n in topo.nodes if not chains_through(topo, n)]
    if unused:
        print(warn(f"\nне входят ни в одну цепочку: {', '.join(unused)}"))
    print(f"\nКлиентов: {len(clients)} (включено {sum(1 for c in clients if c.enabled)})")
    return 0


def cmd_bootstrap(args, ws: Workspace) -> int:
    topo, _, _ = ws.load()
    key = Path(args.key).expanduser() if args.key else next((p for p in (Path.home() / ".ssh/id_ed25519.pub", Path.home() / ".ssh/id_rsa.pub") if p.exists()), None)
    if not key or not key.exists():
        raise SpecError("не найден публичный ключ, укажите --key путь.pub")
    failed = 0
    for name in pick_nodes(topo, args.node):
        node = topo.nodes[name]
        reachable, _ = Remote(node).reachable()
        if reachable:
            print(f"{name}: вход по ключу уже работает")
            continue
        password = os.environ.get(node.password_env or "") if node.password_env else None
        if not password:
            password = getpass.getpass(f"пароль root для {name} ({node.host}): ")
        try:
            install_key(node, password, key)
        except RemoteError as exc:
            print(bad(f"{name}: {exc} {exc.output}"))
            failed += 1
            continue
        reachable, why = Remote(node).reachable()
        print(ok(f"{name}: ключ установлен") if reachable else bad(f"{name}: ключ добавлен, но вход не работает: {why}"))
        failed += 0 if reachable else 1
    return 1 if failed else 0


def cmd_apply(args, ws: Workspace) -> int:
    from .ops import run_apply

    code, _ = run_apply(ws, args.node, args.dry_run, log=print)
    if code or args.dry_run or args.no_verify:
        return code
    return cmd_verify(argparse.Namespace(chain=None), ws)


def cmd_verify(args, ws: Workspace) -> int:
    topo, clients, state = ws.load()
    state.ensure(topo, clients)
    bad_count = 0
    for chain in topo.chains.values():
        if args.chain and chain.name != args.chain:
            continue
        probe = next((c for c in clients if c.enabled and chain.name in c.chains), None)
        if not probe:
            print(warn(f"{chain.name}: нет включённых клиентов, проверять нечем"))
            continue
        try:
            passed, got = verify_chain(topo, state, chain, probe.name)
        except (RemoteError, subprocess.TimeoutExpired) as exc:
            passed, got = False, f"ошибка: {exc}"
        route = " -> ".join(chain.path)
        if passed:
            print(ok(f"{chain.name}: {route}  выход в сеть с {got}"))
        else:
            print(bad(f"{chain.name}: {route}  не прошла (получено '{got}', ожидалось {topo.nodes[chain.exit].host})"))
            bad_count += 1
    return 1 if bad_count else 0


def cmd_status(args, ws: Workspace) -> int:
    topo, clients, state = ws.load()
    state.ensure(topo, clients)
    rows = []
    problems = 0
    for name in apply_order(topo):
        node = topo.nodes[name]
        try:
            st = node_status(node)
        except (RemoteError, subprocess.TimeoutExpired) as exc:
            rows.append([name, node.host, bad("нет доступа"), "", "", "", "", str(exc)[:40]])
            problems += 1
            continue
        want = config_digest(render(build_node_config(topo, clients, state, name)))
        have = st["marker"].get("config", "")
        active = st.get("active") == "active" and st.get("listen", "0") != "0"
        in_sync = have == want
        if not active or not in_sync:
            problems += 1
        rows.append([
            name, node.host,
            ok("работает") if active else bad(st.get("active", "?")),
            st.get("xray", "?"), f"{st.get('swap_mb', '?')} МБ", st.get("cc", "?"),
            f"{st.get('mem_mb', '?')} МБ свободно, load {st.get('load', '?')}, диск {st.get('disk', '?')}",
            ok("актуален") if in_sync else warn("расходится, нужен apply"),
        ])
    print(table(rows, ["сервер", "адрес", "Xray", "версия", "swap", "tcp", "ресурсы", "конфиг"]))
    return 1 if problems else 0


def cmd_chain(args, ws: Workspace) -> int:
    act = args.action
    if act == "list":
        topo, clients, _ = ws.load()
        rows = [[c.name, " -> ".join(c.path) + " -> интернет", c.title, str(sum(1 for x in clients if c.name in x.chains))] for c in topo.chains.values()]
        print(table(rows, ["цепочка", "путь", "название", "клиентов"]))
        return 0
    if act == "add":
        if len(args.nodes) < 2:
            raise SpecError("укажите серверы по порядку: вход, звенья, выход (последний выходит в интернет), например: chain add s1 s2 s3")
        name = ops.chain_build(ws, args.nodes, args.title or "", args.name or "")
        print(ok(f"цепочка {name}: " + " -> ".join(args.nodes) + " -> интернет"))
        print("дальше: lerk-vpn client add имя --chain " + name + ", затем lerk-vpn apply")
        return 0
    if len(args.nodes) != 1:
        raise SpecError("укажите имя цепочки")
    ops.chain_delete(ws, args.nodes[0])
    print(ok(f"цепочка {args.nodes[0]} удалена; lerk-vpn apply уберёт её с серверов"))
    return 0


def cmd_client(args, ws: Workspace) -> int:
    topo, clients, state = ws.load()
    by_name = {c.name: c for c in clients}
    act = args.action
    if act == "list":
        rows = [[c.name, ", ".join(c.chains), ok("вкл") if c.enabled else warn("выкл"), c.note] for c in clients]
        print(table(rows, ["клиент", "цепочки", "статус", "заметка"]) if rows else "клиентов нет")
        return 0
    if act != "add" and args.name not in by_name:
        raise SpecError(f"клиента '{args.name}' нет")
    if act == "add":
        from .model import _check_name

        _check_name("клиент", args.name)
        if args.name in by_name:
            raise SpecError(f"клиент '{args.name}' уже есть")
        chains = args.chain or ([next(iter(topo.chains))] if len(topo.chains) == 1 else [])
        if not chains:
            raise SpecError("укажите цепочку: --chain имя")
        for c in chains:
            if c not in topo.chains:
                raise SpecError(f"цепочки '{c}' нет")
        clients.append(Client(args.name, list(dict.fromkeys(chains)), True, args.note or ""))
    elif act == "rm":
        clients = [c for c in clients if c.name != args.name]
        state.forget_client(args.name)
    elif act in ("enable", "disable"):
        by_name[args.name].enabled = act == "enable"
    elif act == "rotate":
        state.rotate_client(args.name)
    save_clients(ws.clients_path, clients)
    state.ensure(topo, clients)
    state.save()
    print(ok(f"готово: {act} {args.name}"))
    if act == "add":
        for link in client_links(topo, state, next(c for c in clients if c.name == args.name)):
            print(link)
    print("чтобы изменения дошли до серверов: lerk-vpn apply")
    return 0


def cmd_links(args, ws: Workspace) -> int:
    topo, clients, state = ws.load()
    state.ensure(topo, clients)
    chosen = [c for c in clients if not args.name or c.name == args.name]
    if args.name and not chosen:
        raise SpecError(f"клиента '{args.name}' нет")
    for c in chosen:
        if not c.enabled:
            continue
        links = client_links(topo, state, c)
        if args.plain:
            print("\n".join(links))
        else:
            print(f"# {c.name}  подписка: /sub/{state.token(c.name)}")
            print("\n".join(links))
    return 0


def cmd_export(args, ws: Workspace) -> int:
    topo, clients, state = ws.load()
    if state.ensure(topo, clients):
        state.save()
    n = write_exports(ws, topo, clients, state)
    print(ok(f"записано {n} клиентов в {ws.out}"))
    return 0


def cmd_web(args, ws: Workspace) -> int:
    if args.web_action == "set-password":
        import getpass as gp

        from .web.auth import hash_password

        p1 = gp.getpass("Новый пароль админа: ")
        if len(p1) < 10:
            raise SpecError("пароль короче 10 символов")
        if p1 != gp.getpass("Повторите пароль: "):
            raise SpecError("пароли не совпали")
        h = hash_password(p1)
        if args.env_file:
            path = Path(args.env_file)
            lines = [ln for ln in (path.read_text("utf-8").splitlines() if path.exists() else []) if not ln.startswith("LERK_VPN_ADMIN_HASH=")]
            lines.append(f"LERK_VPN_ADMIN_HASH={h}")
            path.write_text("\n".join(lines) + "\n", "utf-8")
            os.chmod(path, 0o600)
            print(ok(f"хеш записан в {path}; перезапустите службу панели, все входы завершатся"))
        else:
            print(h)
        return 0
    from .web.app import run

    return run(ws.root, args.host, args.port)


def cmd_serve(args, ws: Workspace) -> int:
    ws.load()
    serve(ws.root, args.host, args.port)
    return 0


# ---------------------------------------------------------------- разбор аргументов

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lerk-vpn", description="Автоматическая установка и настройка каскадных VPN (Xray, VLESS + Reality)")
    p.add_argument("-C", "--dir", default=os.environ.get("LERK_VPN_DIR", "."), help="рабочая папка с topology.toml (по умолчанию текущая)")
    p.add_argument("--version", action="version", version=f"lerk-vpn {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="создать рабочую папку с примером topology.toml")
    s.add_argument("path", nargs="?", help="папка (по умолчанию --dir)")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("plan", help="проверить описание и показать, что будет настроено")
    s.add_argument("--show-config", metavar="СЕРВЕР", help="напечатать итоговый конфиг Xray для сервера")
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("bootstrap", help="положить SSH-ключ на серверы, входя по паролю")
    s.add_argument("node", nargs="*")
    s.add_argument("--key", help="публичный ключ (по умолчанию ~/.ssh/id_ed25519.pub)")
    s.set_defaults(fn=cmd_bootstrap)

    s = sub.add_parser("apply", help="установить и настроить серверы, выпустить ссылки")
    s.add_argument("node", nargs="*", help="только эти серверы")
    s.add_argument("--dry-run", action="store_true", help="ничего не менять на серверах")
    s.add_argument("--no-verify", action="store_true", help="не проверять цепочки после установки")
    s.set_defaults(fn=cmd_apply)

    s = sub.add_parser("verify", help="проверить каждую цепочку сквозным запросом")
    s.add_argument("--chain")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("status", help="состояние серверов и совпадение конфигов")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("chain", help="цепочки: add (любое число серверов, последний выходной), list, rm")
    s.add_argument("action", choices=["add", "list", "rm"])
    s.add_argument("nodes", nargs="*", help="add: серверы по порядку вход ... выход; rm: имя цепочки")
    s.add_argument("--name", help="имя цепочки (по умолчанию из имён серверов)")
    s.add_argument("--title", help="название, которое видит клиент в Happ")
    s.set_defaults(fn=cmd_chain)

    s = sub.add_parser("client", help="клиенты: add, rm, enable, disable, rotate, list")
    s.add_argument("action", choices=["add", "rm", "enable", "disable", "rotate", "list"])
    s.add_argument("name", nargs="?")
    s.add_argument("--chain", action="append", help="цепочка клиента (можно несколько раз)")
    s.add_argument("--note", help="заметка (кому выдан)")
    s.set_defaults(fn=cmd_client)

    s = sub.add_parser("links", help="напечатать ссылки клиента (или всех)")
    s.add_argument("name", nargs="?")
    s.add_argument("--plain", action="store_true", help="только ссылки, без заголовков")
    s.set_defaults(fn=cmd_links)

    s = sub.add_parser("export", help="записать ссылки и подписки в out/")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("web", help="веб-панель: run (запуск) или set-password (пароль админа)")
    s.add_argument("web_action", choices=["run", "set-password"])
    s.add_argument("--host", default=os.environ.get("LERK_VPN_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("LERK_VPN_PORT", "8780")))
    s.add_argument("--env-file", help="для set-password: записать хеш в этот env-файл")
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("serve", help="отдавать подписки по HTTP (/sub/<токен>)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8088)
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(getattr(args, "path", None) or args.dir).expanduser().resolve()
    ws = Workspace(root)
    if args.cmd == "client" and args.action != "list" and not args.name:
        print("укажите имя клиента", file=sys.stderr)
        return 2
    try:
        return args.fn(args, ws)
    except SpecError as exc:
        print(bad(f"ошибка в описании: {exc}"), file=sys.stderr)
        return 2
    except RemoteError as exc:
        print(bad(f"ошибка сервера: {exc}\n{exc.output}"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
