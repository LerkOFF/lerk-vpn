"""Секреты и идентификаторы, которые инструмент выпускает сам (state.json, права 600)."""

from __future__ import annotations

import json
import os
import secrets
import uuid
from pathlib import Path

from .model import Client, Topology
from .x25519 import new_reality_keypair


class State:
    def __init__(self, path: Path, data: dict | None = None):
        self.path = path
        self.data = data or {"version": 1, "nodes": {}, "hops": {}, "clients": {}}

    @classmethod
    def load(cls, path: Path) -> "State":
        if path.exists():
            return cls(path, json.loads(path.read_text("utf-8")))
        return cls(path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, self.path)

    # --- чтение ---
    def node(self, name: str) -> dict:
        return self.data["nodes"][name]

    def hop_uuid(self, chain: str, index: int) -> str:
        return self.data["hops"][f"{chain}/{index}"]

    def client_uuid(self, client: str, chain: str) -> str:
        return self.data["clients"][client]["uuids"][chain]

    def token(self, client: str) -> str:
        return self.data["clients"][client]["token"]

    def client_by_token(self, token: str) -> str | None:
        for name, item in self.data["clients"].items():
            if secrets.compare_digest(item["token"], token):
                return name
        return None

    # --- выпуск недостающего ---
    def ensure(self, topo: Topology, clients: list[Client]) -> bool:
        changed = False
        for name in topo.nodes:
            if name not in self.data["nodes"]:
                private, public = new_reality_keypair()
                self.data["nodes"][name] = {"private_key": private, "public_key": public, "short_id": secrets.token_hex(8)}
                changed = True
        for chain in topo.chains.values():
            for i in range(len(chain.path) - 1):
                key = f"{chain.name}/{i}"
                if key not in self.data["hops"]:
                    self.data["hops"][key] = str(uuid.uuid4())
                    changed = True
        for client in clients:
            item = self.data["clients"].setdefault(client.name, {"token": secrets.token_hex(12), "uuids": {}})
            if "token" not in item:
                item["token"] = secrets.token_hex(12)
                changed = True
            for chain in client.chains:
                if chain not in item["uuids"]:
                    item["uuids"][chain] = str(uuid.uuid4())
                    changed = True
        if changed or not self.path.exists():
            changed = True
        return changed

    def forget_client(self, name: str) -> None:
        self.data["clients"].pop(name, None)

    def rotate_client(self, name: str) -> None:
        item = self.data["clients"].get(name)
        if item:
            item["uuids"] = {}
            item["token"] = secrets.token_hex(12)
