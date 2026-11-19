"""Описание топологии (topology.toml) и список клиентов (clients.json)."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.:_-]*[A-Za-z0-9])?$")
SNI_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
ROLES = {"", "entry", "exit"}
FINGERPRINTS = {"chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random", "randomized"}

NODE_DEFAULTS = {
    "ssh_user": "root",
    "ssh_port": 22,
    "port": 443,
    "sni": "dl.google.com",
    "swap_gb": 4,
    "bbr": True,
    "firewall": False,
    "block_torrents": True,
    "fingerprint": "chrome",
    "flow": "xtls-rprx-vision",
    "mux": False,
    "mux_concurrency": 8,
}
FLOWS = {"xtls-rprx-vision", ""}


class SpecError(ValueError):
    """Ошибка в описании топологии или клиентов."""


@dataclass(frozen=True)
class Node:
    name: str
    host: str
    public_host: str
    ssh_alias: str | None
    ssh_user: str
    ssh_port: int
    port: int
    sni: str
    country: str
    swap_gb: int
    bbr: bool
    firewall: bool
    block_torrents: bool
    fingerprint: str
    flow: str
    mux: bool
    mux_concurrency: int
    password_env: str | None
    role: str = ""


@dataclass(frozen=True)
class Chain:
    name: str
    path: tuple[str, ...]
    title: str

    @property
    def entry(self) -> str:
        return self.path[0]

    @property
    def exit(self) -> str:
        return self.path[-1]


@dataclass
class Client:
    name: str
    chains: list[str]
    enabled: bool = True
    note: str = ""


@dataclass
class Topology:
    project: str
    nodes: dict[str, Node]
    chains: dict[str, Chain]
    xray_version: str | None = None


def _check_name(kind: str, value: str) -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise SpecError(f"{kind} '{value}': допустимы строчные латинские буквы, цифры, '-' и '_', до 32 символов")
    return value


def _int(where: str, value, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise SpecError(f"{where}: нужно целое число от {lo} до {hi}")
    return value


def _bool(where: str, value) -> bool:
    if not isinstance(value, bool):
        raise SpecError(f"{where}: нужно true или false")
    return value


def parse_topology(data: dict, allow_empty: bool = False) -> Topology:
    project = _check_name("project", data.get("project", "main"))
    defaults = {**NODE_DEFAULTS, **(data.get("defaults") or {})}
    unknown_def = set(defaults) - set(NODE_DEFAULTS)
    if unknown_def:
        raise SpecError(f"[defaults]: неизвестные ключи {sorted(unknown_def)}")

    raw_nodes = data.get("nodes") or {}
    if not raw_nodes and not allow_empty:
        raise SpecError("нужен хотя бы один сервер в [nodes.<имя>]")
    nodes: dict[str, Node] = {}
    allowed = set(NODE_DEFAULTS) | {"host", "public_host", "ssh", "country", "password_env", "role"}
    for name, raw in raw_nodes.items():
        _check_name("сервер", name)
        extra = set(raw) - allowed
        if extra:
            raise SpecError(f"nodes.{name}: неизвестные ключи {sorted(extra)}")
        merged = {**defaults, **raw}
        host = merged.get("host")
        if not host or not HOST_RE.match(str(host)):
            raise SpecError(f"nodes.{name}.host: нужен IP или домен")
        public_host = merged.get("public_host") or host
        if not HOST_RE.match(str(public_host)):
            raise SpecError(f"nodes.{name}.public_host: некорректное имя")
        alias = merged.get("ssh")
        if alias is not None and not re.match(r"^[A-Za-z0-9._-]+$", str(alias)):
            raise SpecError(f"nodes.{name}.ssh: алиас из ~/.ssh/config без пробелов")
        sni = str(merged["sni"])
        if not SNI_RE.match(sni):
            raise SpecError(f"nodes.{name}.sni: нужно имя сайта, например dl.google.com")
        fp = str(merged["fingerprint"])
        if fp not in FINGERPRINTS:
            raise SpecError(f"nodes.{name}.fingerprint: одно из {sorted(FINGERPRINTS)}")
        flow = str(merged["flow"])
        if flow not in FLOWS:
            raise SpecError(f"nodes.{name}.flow: xtls-rprx-vision или пусто (без flow)")
        mux = _bool(f"nodes.{name}.mux", merged["mux"])
        if mux and flow:
            raise SpecError(f"nodes.{name}: мультиплексирование (mux) работает только без flow, уберите xtls-rprx-vision")
        penv = merged.get("password_env")
        if penv is not None and not re.match(r"^[A-Z_][A-Z0-9_]*$", str(penv)):
            raise SpecError(f"nodes.{name}.password_env: имя переменной окружения")
        role = str(merged.get("role", "") or "")
        if role not in ROLES:
            raise SpecError(f"nodes.{name}.role: пусто, entry (сервер 1) или exit (сервер 2)")
        user = str(merged["ssh_user"])
        if not re.match(r"^[a-z_][a-z0-9_-]*$", user):
            raise SpecError(f"nodes.{name}.ssh_user: некорректный логин")
        nodes[name] = Node(
            name=name,
            host=str(host),
            public_host=str(public_host),
            ssh_alias=str(alias) if alias else None,
            ssh_user=user,
            ssh_port=_int(f"nodes.{name}.ssh_port", merged["ssh_port"], 1, 65535),
            port=_int(f"nodes.{name}.port", merged["port"], 1, 65535),
            sni=sni,
            country=str(merged.get("country", "")).upper()[:2],
            swap_gb=_int(f"nodes.{name}.swap_gb", merged["swap_gb"], 0, 64),
            bbr=_bool(f"nodes.{name}.bbr", merged["bbr"]),
            firewall=_bool(f"nodes.{name}.firewall", merged["firewall"]),
            block_torrents=_bool(f"nodes.{name}.block_torrents", merged["block_torrents"]),
            fingerprint=fp,
            flow=flow,
            mux=mux,
            mux_concurrency=_int(f"nodes.{name}.mux_concurrency", merged["mux_concurrency"], 1, 1024),
            password_env=str(penv) if penv else None,
            role=role,
        )

    hosts: dict[str, str] = {}
    for n in nodes.values():
        if n.host in hosts:
            raise SpecError(f"серверы {hosts[n.host]} и {n.name} указывают на один адрес {n.host}: один сервер описывается один раз")
        hosts[n.host] = n.name

    raw_chains = data.get("chains") or {}
    if not raw_chains and not allow_empty:
        raise SpecError("нужна хотя бы одна цепочка в [chains.<имя>]")
    chains: dict[str, Chain] = {}
    for name, raw in raw_chains.items():
        _check_name("цепочка", name)
        extra = set(raw) - {"path", "title"}
        if extra:
            raise SpecError(f"chains.{name}: неизвестные ключи {sorted(extra)}")
        path = raw.get("path")
        if not isinstance(path, list) or not path:
            raise SpecError(f"chains.{name}.path: список серверов от входа к выходу")
        for hop in path:
            if hop not in nodes:
                raise SpecError(f"chains.{name}: сервер '{hop}' не описан в [nodes]")
        if len(set(path)) != len(path):
            raise SpecError(f"chains.{name}: сервер встречается в цепочке дважды")
        chains[name] = Chain(name=name, path=tuple(path), title=str(raw.get("title") or name))

    version = data.get("xray_version")
    if version is not None and not re.match(r"^\d+\.\d+\.\d+$", str(version)):
        raise SpecError("xray_version: формат 26.11.9")
    return Topology(project=project, nodes=nodes, chains=chains, xray_version=str(version) if version else None)


def load_topology(path: Path, allow_empty: bool = False) -> Topology:
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise SpecError(f"нет файла {path}; создайте рабочую папку командой: lerk-vpn init") from None
    except tomllib.TOMLDecodeError as exc:
        raise SpecError(f"{path}: {exc}") from None
    return parse_topology(data, allow_empty)


def load_clients(path: Path, topo: Topology) -> list[Client]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecError(f"{path}: {exc}") from None
    result: list[Client] = []
    seen: set[str] = set()
    for item in raw.get("clients", []):
        name = _check_name("клиент", item.get("name"))
        if name in seen:
            raise SpecError(f"клиент '{name}' указан дважды")
        seen.add(name)
        chains = item.get("chains") or []
        if not chains:
            raise SpecError(f"клиент '{name}': не указана цепочка")
        for c in chains:
            if c not in topo.chains:
                raise SpecError(f"клиент '{name}': цепочки '{c}' нет в топологии")
        result.append(Client(name=name, chains=list(dict.fromkeys(chains)), enabled=bool(item.get("enabled", True)), note=str(item.get("note", ""))))
    return result


def save_clients(path: Path, clients: list[Client]) -> None:
    payload = {"clients": [{"name": c.name, "chains": c.chains, "enabled": c.enabled, "note": c.note} for c in clients]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    tmp.replace(path)
