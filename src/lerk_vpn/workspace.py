"""Рабочая папка: topology.toml, clients.json, state.json, out/. Общая для CLI и веб-панели."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import os
from pathlib import Path

from .model import Client, SpecError, Topology, load_clients, load_topology
from .state import State
from .xraycfg import apply_order, client_links


class Workspace:
    def __init__(self, root: Path, allow_empty: bool = False):
        self.root = root
        self.allow_empty = allow_empty
        self.topology_path = root / "topology.toml"
        self.clients_path = root / "clients.json"
        self.state_path = root / "state.json"
        self.out = root / "out"
        self.lock_path = root / ".lock"

    def load(self) -> tuple[Topology, list[Client], State]:
        topo = load_topology(self.topology_path, self.allow_empty)
        clients = load_clients(self.clients_path, topo)
        state = State.load(self.state_path)
        return topo, clients, state

    @contextlib.contextmanager
    def lock(self):
        """Не даёт CLI и панели одновременно менять рабочую папку."""
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def pick_nodes(topo: Topology, names: list[str] | None) -> list[str]:
    order = apply_order(topo)
    if not names:
        return order
    for n in names:
        if n not in topo.nodes:
            raise SpecError(f"сервера '{n}' нет в topology.toml")
    return [n for n in order if n in names]


def write_exports(ws: Workspace, topo: Topology, clients: list[Client], state: State) -> int:
    sub = ws.out / "subscriptions"
    sub.mkdir(parents=True, exist_ok=True)
    for old in sub.glob("*"):
        old.unlink()
    count = 0
    index = []
    for client in clients:
        if not client.enabled:
            continue
        links = client_links(topo, state, client)
        text = "\n".join(links) + "\n"
        (sub / f"{client.name}.txt").write_text(text, "utf-8")
        (sub / f"{client.name}.b64").write_text(base64.b64encode(text.encode()).decode(), "utf-8")
        index.append(f"{client.name}\t/sub/{state.token(client.name)}")
        count += 1
    (ws.out / "index.tsv").write_text("\n".join(index) + ("\n" if index else ""), "utf-8")
    for p in [ws.out, sub, *sub.glob("*"), ws.out / "index.tsv"]:
        os.chmod(p, 0o700 if p.is_dir() else 0o600)
    return count
