"""Операции над рабочей папкой. Их вызывают и CLI, и веб-панель: так поведение одинаковое."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .model import Client, SpecError, _check_name, load_clients, save_clients
from .provision import apply_node, verify_chain
from .remote import Remote, RemoteError, install_key
from .state import State
from .tomlw import load_raw, save_raw
from .workspace import Workspace, pick_nodes, write_exports
from .xraycfg import build_node_config


def run_apply(ws: Workspace, names: list[str] | None = None, dry_run: bool = False, log=print) -> tuple[int, list]:
    """Приводит серверы к описанию. Возвращает (код, результаты)."""
    from .provision import config_digest, render
    from .xraycfg import users_at

    with ws.lock():
        topo, clients, state = ws.load()
        if state.ensure(topo, clients):
            state.save()
        order = pick_nodes(topo, names)
        configs = {n: build_node_config(topo, clients, state, n) for n in order}
    if dry_run:
        for n in order:
            log(f"{n}: конфиг {config_digest(render(configs[n]))}, пользователей {len(users_at(topo, clients, state, n))}")
        log("dry-run: на серверах ничего не менялось")
        return 0, []
    log("Проверяю доступ по SSH...")
    dead = []
    for n in order:
        reachable, why = Remote(topo.nodes[n]).reachable()
        log(f"  {n}: {'ok' if reachable else 'нет доступа: ' + why}")
        if not reachable:
            dead.append(n)
    if dead:
        log(f"нет доступа к {', '.join(dead)}")
        return 1, []
    results = []
    for n in order:
        log(f"Сервер {n} ({topo.nodes[n].host})")
        try:
            results.append(apply_node(topo, topo.nodes[n], configs[n], log=log))
        except RemoteError as exc:
            log(f"{n}: {exc}\n{exc.output}")
            log("остановлено: остальные серверы не тронуты")
            return 1, results
    for r in results:
        log(f"  {r.node}: Xray {r.xray}, конфиг {'обновлён' if r.changed else 'без изменений'}")
    with ws.lock():
        topo, clients, state = ws.load()
        count = write_exports(ws, topo, clients, state)
    log(f"Ссылки клиентов обновлены ({count})")
    return 0, results


def run_verify(ws: Workspace, chain_name: str | None = None, log=print) -> tuple[int, list[tuple[str, bool, str]]]:
    topo, clients, state = ws.load()
    state.ensure(topo, clients)
    outcomes = []
    for chain in topo.chains.values():
        if chain_name and chain.name != chain_name:
            continue
        probe = next((c for c in clients if c.enabled and chain.name in c.chains), None)
        if not probe:
            log(f"{chain.name}: нет включённых клиентов, проверять нечем")
            continue
        try:
            passed, got = verify_chain(topo, state, chain, probe.name)
        except (RemoteError, subprocess.TimeoutExpired) as exc:
            passed, got = False, f"ошибка: {exc}"
        route = " -> ".join(chain.path)
        log(f"{chain.name}: {route}  " + (f"выход в сеть с {got}" if passed else f"не прошла (получено '{got}', ожидалось {topo.nodes[chain.exit].host})"))
        outcomes.append((chain.name, passed, got))
    return (0 if all(o[1] for o in outcomes) else 1), outcomes


# ------------------------------------------------------------ клиенты

def client_add(ws: Workspace, name: str, chains: list[str], note: str = "") -> None:
    with ws.lock():
        topo, clients, state = ws.load()
        _check_name("клиент", name)
        if any(c.name == name for c in clients):
            raise SpecError(f"клиент '{name}' уже есть")
        chains = list(dict.fromkeys(chains))
        if not chains:
            raise SpecError("выберите хотя бы одну цепочку")
        for c in chains:
            if c not in topo.chains:
                raise SpecError(f"цепочки '{c}' нет")
        clients.append(Client(name, chains, True, note.strip()[:200]))
        save_clients(ws.clients_path, clients)
        state.ensure(topo, clients)
        state.save()


def client_update(ws: Workspace, name: str, *, enabled: bool | None = None, chains: list[str] | None = None, note: str | None = None) -> None:
    with ws.lock():
        topo, clients, state = ws.load()
        client = next((c for c in clients if c.name == name), None)
        if not client:
            raise SpecError(f"клиента '{name}' нет")
        if enabled is not None:
            client.enabled = enabled
        if chains is not None:
            chains = list(dict.fromkeys(chains))
            if not chains:
                raise SpecError("у клиента должна остаться хотя бы одна цепочка")
            for c in chains:
                if c not in topo.chains:
                    raise SpecError(f"цепочки '{c}' нет")
            client.chains = chains
        if note is not None:
            client.note = note.strip()[:200]
        save_clients(ws.clients_path, clients)
        state.ensure(topo, clients)
        state.save()


def client_rotate(ws: Workspace, name: str) -> None:
    with ws.lock():
        topo, clients, state = ws.load()
        if not any(c.name == name for c in clients):
            raise SpecError(f"клиента '{name}' нет")
        state.rotate_client(name)
        state.ensure(topo, clients)
        state.save()


def client_delete(ws: Workspace, name: str) -> None:
    with ws.lock():
        topo, clients, state = ws.load()
        if not any(c.name == name for c in clients):
            raise SpecError(f"клиента '{name}' нет")
        clients = [c for c in clients if c.name != name]
        state.forget_client(name)
        save_clients(ws.clients_path, clients)
        state.ensure(topo, clients)
        state.save()


# ------------------------------------------------------------ серверы и цепочки

def next_node_name(existing) -> str:
    n = 1
    while f"s{n}" in existing:
        n += 1
    return f"s{n}"


def server_add(ws: Workspace, host: str, user: str, password: str, role: str, pubkey: Path, port: int = 22, log=print, sni: str | None = None, swap_gb: int | None = None) -> str:
    """Кладёт ключ панели на сервер (вход по паролю), определяет страну, записывает сервер в topology.toml."""
    from .model import HOST_RE, parse_topology

    host = host.strip()
    if not HOST_RE.match(host):
        raise SpecError("адрес сервера: IP или домен без пробелов")
    if role not in ("entry", "exit", ""):
        raise SpecError("роль: сервер 1 или сервер 2")
    if not re.match(r"^[a-z_][a-z0-9_-]*$", user):
        raise SpecError("логин: строчные латинские буквы, цифры, '-' и '_'")
    raw = load_raw(ws.topology_path)
    for n in (raw.get("nodes") or {}).values():
        if n.get("host") == host:
            raise SpecError(f"сервер {host} уже добавлен")
    defaults = {**(raw.get("defaults") or {})}
    probe_node = parse_topology({"nodes": {"probe": {"host": host, "ssh_user": user, "ssh_port": port}}, "chains": {"c": {"path": ["probe"]}}}).nodes["probe"]
    log(f"Вхожу на {host} по паролю и кладу ключ панели...")
    reachable, _ = Remote(probe_node).reachable()
    if reachable:
        log("вход по ключу уже работает, пароль не нужен")
    else:
        install_key(probe_node, password, pubkey)
        reachable, why = Remote(probe_node).reachable()
        if not reachable:
            raise RemoteError(f"ключ добавлен, но вход по нему не работает: {why}")
        log("ключ установлен, вход по ключу работает")
    country = ""
    try:
        res = Remote(probe_node).run("curl -s -m 8 https://ipinfo.io/country 2>/dev/null | tr -d '[:space:]'", timeout=30, check=False)
        c = res.out.strip().upper()
        country = c if re.match(r"^[A-Z]{2}$", c) else ""
    except subprocess.TimeoutExpired:
        pass
    with ws.lock():
        raw = load_raw(ws.topology_path)
        nodes = raw.setdefault("nodes", {})
        name = next_node_name(nodes)
        entry = {"host": host}
        if user != defaults.get("ssh_user", "root"):
            entry["ssh_user"] = user
        if port != defaults.get("ssh_port", 22):
            entry["ssh_port"] = port
        if country:
            entry["country"] = country
        if sni and sni != defaults.get("sni", "dl.google.com"):
            entry["sni"] = sni
        if swap_gb is not None and swap_gb != defaults.get("swap_gb", 4):
            entry["swap_gb"] = swap_gb
        if role:
            entry["role"] = role
        nodes[name] = entry
        save_raw(ws.topology_path, raw, ws.allow_empty)
    log(f"Сервер добавлен как {name}" + (f", страна {country}" if country else ""))
    return name


def server_delete(ws: Workspace, name: str) -> None:
    with ws.lock():
        raw = load_raw(ws.topology_path)
        if name not in (raw.get("nodes") or {}):
            raise SpecError(f"сервера '{name}' нет")
        for chain, c in (raw.get("chains") or {}).items():
            if name in c.get("path", []):
                raise SpecError(f"сервер входит в цепочку '{chain}', сначала удалите её")
        del raw["nodes"][name]
        if not raw["nodes"] and not ws.allow_empty:
            raise SpecError("нельзя удалить последний сервер")
        save_raw(ws.topology_path, raw, ws.allow_empty)


def server_set_role(ws: Workspace, name: str, role: str) -> None:
    with ws.lock():
        raw = load_raw(ws.topology_path)
        if name not in (raw.get("nodes") or {}):
            raise SpecError(f"сервера '{name}' нет")
        if role:
            raw["nodes"][name]["role"] = role
        else:
            raw["nodes"][name].pop("role", None)
        save_raw(ws.topology_path, raw, ws.allow_empty)


def cascade_create(ws: Workspace, entries: list[str], exit_node: str) -> list[str]:
    """Несколько серверов 1 и один сервер 2: на каждый сервер 1 по цепочке до сервера 2."""
    if not entries:
        raise SpecError("выберите хотя бы один сервер 1")
    if not exit_node:
        raise SpecError("выберите сервер 2")
    with ws.lock():
        raw = load_raw(ws.topology_path)
        nodes = raw.get("nodes") or {}
        for n in [*entries, exit_node]:
            if n not in nodes:
                raise SpecError(f"сервера '{n}' нет")
        if exit_node in entries:
            raise SpecError("сервер 2 не может быть одновременно сервером 1")
        chains = raw.setdefault("chains", {})
        created = []
        for e in dict.fromkeys(entries):
            existing = next((k for k, c in chains.items() if c.get("path") == [e, exit_node]), None)
            if existing:
                continue
            name = f"{e}-{exit_node}"
            suffix = 2
            while name in chains:
                name = f"{e}-{exit_node}-{suffix}"
                suffix += 1
            ce, cx = nodes[e].get("country", ""), nodes[exit_node].get("country", "")
            chains[name] = {"path": [e, exit_node], "title": f"{ce}-{cx}" if ce and cx else name}
            created.append(name)
        if not created:
            raise SpecError("такие каскады уже есть")
        save_raw(ws.topology_path, raw, ws.allow_empty)
    return created


def chain_delete(ws: Workspace, name: str) -> None:
    with ws.lock():
        raw = load_raw(ws.topology_path)
        if name not in (raw.get("chains") or {}):
            raise SpecError(f"цепочки '{name}' нет")
        if len(raw["chains"]) == 1 and (not ws.allow_empty or load_clients_lenient(ws)):
            raise SpecError("это последняя цепочка: сначала удалите клиентов или создайте другую цепочку")
        del raw["chains"][name]
        save_raw(ws.topology_path, raw, ws.allow_empty)
        topo = None
        from .model import parse_topology

        topo = parse_topology(raw, ws.allow_empty)
        clients = load_clients_lenient(ws)
        kept = []
        for c in clients:
            c.chains = [x for x in c.chains if x != name]
            if c.chains:
                kept.append(c)
            else:
                # клиент остался без цепочек: отключаем, привязываем к первой существующей
                c.chains = [next(iter(topo.chains))]
                c.enabled = False
                kept.append(c)
        save_clients(ws.clients_path, kept)


def load_clients_lenient(ws: Workspace) -> list[Client]:
    import json

    if not ws.clients_path.exists():
        return []
    raw = json.loads(ws.clients_path.read_text("utf-8"))
    return [Client(i["name"], list(i.get("chains") or []), bool(i.get("enabled", True)), str(i.get("note", ""))) for i in raw.get("clients", [])]
