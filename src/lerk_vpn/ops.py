"""Операции над рабочей папкой. Их вызывают и CLI, и веб-панель: так поведение одинаковое."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .model import NAME_RE, NODE_DEFAULTS, Client, SpecError, _check_name, load_clients, save_clients
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


def client_rename(ws: Workspace, old: str, new: str) -> None:
    """Меняет имя клиента; токен подписки и ключи сохраняются, ссылки продолжают работать."""
    new = new.strip().lower()
    _check_name("клиент", new)
    with ws.lock():
        topo, clients, state = ws.load()
        client = next((c for c in clients if c.name == old), None)
        if not client:
            raise SpecError(f"клиента '{old}' нет")
        if new == old:
            return
        if any(c.name == new for c in clients):
            raise SpecError(f"клиент '{new}' уже есть")
        client.name = new
        if old in state.data["clients"]:
            state.data["clients"][new] = state.data["clients"].pop(old)
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


def chains_of_server(ws: Workspace, name: str) -> list[str]:
    raw = load_raw(ws.topology_path)
    return [k for k, c in (raw.get("chains") or {}).items() if name in c.get("path", [])]


def server_delete(ws: Workspace, name: str, with_chains: bool = False) -> list[str]:
    """Убирает сервер из панели. Если он в каскадах, их надо удалить вместе с ним (with_chains).
    Возвращает удалённые каскады. Сам сервер и Xray на нём не трогаются."""
    used = chains_of_server(ws, name)
    if used and not with_chains:
        raise SpecError(f"сервер входит в каскады {', '.join(used)}: удалите их или выберите «удалить вместе с каскадами»")
    for chain in used:
        chain_delete(ws, chain)
    with ws.lock():
        raw = load_raw(ws.topology_path)
        if name not in (raw.get("nodes") or {}):
            raise SpecError(f"сервера '{name}' нет")
        del raw["nodes"][name]
        if not raw["nodes"] and not ws.allow_empty:
            raise SpecError("нельзя удалить последний сервер")
        save_raw(ws.topology_path, raw, ws.allow_empty)
    return used


def server_rename(ws: Workspace, old: str, new: str, country: str | None = None) -> None:
    """Меняет имя сервера (во всех каскадах и в ключах) и при необходимости страну."""
    new = new.strip().lower()
    _check_name("сервер", new)
    with ws.lock():
        raw = load_raw(ws.topology_path)
        nodes = raw.get("nodes") or {}
        if old not in nodes:
            raise SpecError(f"сервера '{old}' нет")
        if new != old:
            if new in nodes:
                raise SpecError(f"сервер '{new}' уже есть")
            raw["nodes"] = {(new if k == old else k): v for k, v in nodes.items()}
            for c in (raw.get("chains") or {}).values():
                c["path"] = [new if n == old else n for n in c.get("path", [])]
        if country is not None:
            cc = country.strip().upper()
            if cc and not re.match(r"^[A-Z]{2}$", cc):
                raise SpecError("страна: две буквы, например FI")
            node = raw["nodes"][new]
            if cc:
                node["country"] = cc
            else:
                node.pop("country", None)
        save_raw(ws.topology_path, raw, ws.allow_empty)
        topo, clients, state = ws.load()
        if new != old and old in state.data["nodes"]:
            state.data["nodes"][new] = state.data["nodes"].pop(old)
        state.ensure(topo, clients)
        state.save()


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


def _num(label: str, text: str, lo: int, hi: int) -> int:
    try:
        v = int(str(text).strip())
    except ValueError:
        raise SpecError(f"{label}: нужно целое число") from None
    if not lo <= v <= hi:
        raise SpecError(f"{label}: от {lo} до {hi}")
    return v


def server_edit(ws: Workspace, name: str, form: dict) -> list[str]:
    """Меняет параметры сервера: порт Xray, SNI, fingerprint, flow, mux, ssh-порт, swap, торренты, файрвол.
    Возвращает названия изменённых параметров; после смены нужен «Применить»."""
    with ws.lock():
        raw = load_raw(ws.topology_path)
        node = (raw.get("nodes") or {}).get(name)
        if node is None:
            raise SpecError(f"сервера '{name}' нет")
        defaults = {**NODE_DEFAULTS, **(raw.get("defaults") or {})}
        new = {
            "port": _num("порт Xray", form.get("port", defaults["port"]), 1, 65535),
            "ssh_port": _num("порт SSH", form.get("ssh_port", defaults["ssh_port"]), 1, 65535),
            "swap_gb": _num("swap, ГБ", form.get("swap_gb", defaults["swap_gb"]), 0, 64),
            "mux_concurrency": _num("потоков в mux", form.get("mux_concurrency", defaults["mux_concurrency"]), 1, 1024),
            "sni": str(form.get("sni", defaults["sni"])).strip().lower(),
            "fingerprint": str(form.get("fingerprint", defaults["fingerprint"])).strip(),
            "flow": "" if form.get("flow") == "none" else "xtls-rprx-vision",
            "mux": bool(form.get("mux")),
            "block_torrents": bool(form.get("block_torrents")),
            "firewall": bool(form.get("firewall")),
        }
        if new["port"] == new["ssh_port"]:
            raise SpecError("порт Xray и порт SSH не должны совпадать")
        for other, o in (raw.get("nodes") or {}).items():
            if other != name and o.get("host") == node.get("host") and o.get("port", defaults["port"]) == new["port"]:
                raise SpecError(f"порт {new['port']} уже занят сервером {other} на этом адресе")
        changed = []
        for key, val in new.items():
            cur = node.get(key, defaults.get(key))
            if cur != val:
                changed.append(key)
            # значения по умолчанию в файле не храним
            if val == defaults.get(key):
                node.pop(key, None)
            else:
                node[key] = val
        if changed:
            save_raw(ws.topology_path, raw, ws.allow_empty)
        return changed


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


def chain_build(ws: Workspace, path: list[str], title: str = "", name: str = "") -> str:
    """Каскад из любого числа серверов: path = [вход, звено, ..., выход]. Последний сервер выходит в интернет.
    Один и тот же сервер может входить в разные каскады, но в одном каскаде только один раз."""
    path = [str(p).strip() for p in path if str(p).strip()]
    if len(path) < 2:
        raise SpecError("в каскаде нужно минимум два сервера: вход и выход (один сервер это прямой доступ)")
    if len(set(path)) != len(path):
        raise SpecError("сервер не может встречаться в каскаде дважды")
    with ws.lock():
        raw = load_raw(ws.topology_path)
        nodes = raw.get("nodes") or {}
        for n in path:
            if n not in nodes:
                raise SpecError(f"сервера '{n}' нет")
        chains = raw.setdefault("chains", {})
        same = next((k for k, c in chains.items() if c.get("path") == path), None)
        if same:
            raise SpecError(f"такой каскад уже есть: {same}")
        base = (name or "-".join(path)).strip().lower()
        if not NAME_RE.match(base):
            base = f"{path[0]}-{path[-1]}-x{len(path)}"[:32]
        final, suffix = base, 2
        while final in chains:
            tail = f"-{suffix}"
            final = base[: 32 - len(tail)] + tail
            suffix += 1
        if not NAME_RE.match(final):
            raise SpecError(f"имя каскада '{final}': строчные латинские буквы, цифры, '-' и '_', до 32 символов")
        labels = [nodes[n].get("country", "") or n for n in path]
        chains[final] = {"path": path, "title": (title.strip() or "-".join(labels))[:40]}
        save_raw(ws.topology_path, raw, ws.allow_empty)
    return final


def direct_create(ws: Workspace, node_names: list[str]) -> list[str]:
    """Прямой доступ без каскада: клиент подключается к одному серверу и выходит в интернет с него."""
    if not node_names:
        raise SpecError("выберите хотя бы один сервер")
    with ws.lock():
        raw = load_raw(ws.topology_path)
        nodes = raw.get("nodes") or {}
        chains = raw.setdefault("chains", {})
        created = []
        for n in dict.fromkeys(node_names):
            if n not in nodes:
                raise SpecError(f"сервера '{n}' нет")
            if any(c.get("path") == [n] for c in chains.values()):
                continue
            name = f"direct-{n}"
            suffix = 2
            while name in chains:
                name = f"direct-{n}-{suffix}"
                suffix += 1
            cc = nodes[n].get("country", "")
            chains[name] = {"path": [n], "title": f"{cc}-direct" if cc else name}
            created.append(name)
        if not created:
            raise SpecError("прямой доступ на выбранных серверах уже есть")
        save_raw(ws.topology_path, raw, ws.allow_empty)
    return created


def chain_edit(ws: Workspace, name: str, title: str, new_name: str | None = None) -> str:
    """Название каскада (видно клиенту в Happ) и, при желании, его идентификатор. Возвращает итоговое имя."""
    title = title.strip()[:40]
    if not title:
        raise SpecError("название не может быть пустым")
    new_name = (new_name or name).strip().lower()
    _check_name("цепочка", new_name)
    with ws.lock():
        raw = load_raw(ws.topology_path)
        chains = raw.get("chains") or {}
        if name not in chains:
            raise SpecError(f"цепочки '{name}' нет")
        if new_name != name and new_name in chains:
            raise SpecError(f"цепочка '{new_name}' уже есть")
        chains[name]["title"] = title
        if new_name != name:
            raw["chains"] = {(new_name if k == name else k): v for k, v in chains.items()}
        save_raw(ws.topology_path, raw, ws.allow_empty)
        if new_name != name:
            clients = load_clients_lenient(ws)
            for c in clients:
                c.chains = [new_name if x == name else x for x in c.chains]
            save_clients(ws.clients_path, clients)
            topo, clients2, state = ws.load()
            for key in [k for k in state.data["hops"] if k.startswith(name + "/")]:
                state.data["hops"][f"{new_name}/{key.split('/', 1)[1]}"] = state.data["hops"].pop(key)
            for item in state.data["clients"].values():
                if name in item.get("uuids", {}):
                    item["uuids"][new_name] = item["uuids"].pop(name)
            state.ensure(topo, clients2)
            state.save()
    return new_name


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
