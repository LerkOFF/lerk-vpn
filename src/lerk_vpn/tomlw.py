"""Запись topology.toml из словаря (панель правит серверы и цепочки)."""

from __future__ import annotations

import json
import shutil
import time
import tomllib
from pathlib import Path

from .model import SpecError, parse_topology


def _val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    raise SpecError(f"нельзя записать значение {v!r}")


def _table(name: str, data: dict) -> str:
    lines = [f"[{name}]"]
    for k, v in data.items():
        lines.append(f"{k} = {_val(v)}")
    return "\n".join(lines)


def dump(raw: dict) -> str:
    out: list[str] = ["# Файл ведётся панелью и командой lerk-vpn; ручные правки допустимы, комментарии не сохраняются."]
    top = {k: v for k, v in raw.items() if not isinstance(v, dict)}
    for k, v in top.items():
        out.append(f"{k} = {_val(v)}")
    if raw.get("defaults"):
        out.append("")
        out.append(_table("defaults", raw["defaults"]))
    for name, node in (raw.get("nodes") or {}).items():
        out.append("")
        out.append(_table(f"nodes.{name}", node))
    for name, chain in (raw.get("chains") or {}).items():
        out.append("")
        out.append(_table(f"chains.{name}", chain))
    return "\n".join(out) + "\n"


def load_raw(path: Path) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def save_raw(path: Path, raw: dict, allow_empty: bool = True) -> None:
    """Проверяет описание, делает копию прежнего файла и записывает новый атомарно."""
    parse_topology(raw, allow_empty)
    text = dump(raw)
    parse_topology(tomllib.loads(text), allow_empty)
    if path.exists():
        backup = path.parent / "backup"
        backup.mkdir(exist_ok=True)
        shutil.copy2(path, backup / f"topology.{time.strftime('%Y%m%d%H%M%S')}.toml")
        old = sorted(backup.glob("topology.*.toml"))
        for f in old[:-20]:
            f.unlink()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)
