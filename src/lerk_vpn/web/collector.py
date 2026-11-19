"""Фоновый сбор: состояние серверов и трафик клиентов по SSH (xray api statsquery)."""

from __future__ import annotations

import subprocess
import threading
import time

from ..provision import config_digest, node_collect, render
from ..remote import RemoteError
from ..workspace import Workspace
from ..xraycfg import build_node_config
from .db import DB
from .settings import Settings


class Collector:
    def __init__(self, ws: Workspace, db: DB, settings: Settings, log=print):
        self.ws, self.db, self.settings, self.log = ws, db, settings, log
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def interval(self) -> int:
        return int(self.settings.get("collector.interval_min")) * 60

    def run_once(self) -> tuple[int, int]:
        """Один проход по серверам. Возвращает (удачных, всего). Параллельный запуск пропускается."""
        if not self._lock.acquire(blocking=False):
            return 0, 0
        started = int(time.time())
        self.db.kv_set("collector.last_start", str(started))
        good = total = 0
        err = ""
        try:
            topo, clients, state = self.ws.load()
            state.ensure(topo, clients)
            for name, node in topo.nodes.items():
                total += 1
                now = int(time.time())
                try:
                    data, stats = node_collect(node)
                except (RemoteError, subprocess.TimeoutExpired, OSError) as exc:
                    msg = str(getattr(exc, "output", "") or exc).strip().splitlines()[-1:] or ["нет ответа"]
                    self.db.x("INSERT INTO sample(ts,node,ok,error) VALUES(?,?,0,?)", (now, name, msg[0][:200]))
                    err = f"{name}: {msg[0][:120]}"
                    continue
                want = config_digest(render(build_node_config(topo, clients, state, name)))
                have = data["marker"].get("config", "")
                self.db.x(
                    "INSERT INTO sample(ts,node,ok,active,in_sync,mem_mb,swap_mb,load,disk_pct,xray) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        now, name, 1,
                        1 if data.get("active") == "active" and data.get("listen", "0") != "0" else 0,
                        1 if have == want else 0,
                        _int(data.get("mem_mb")), _int(data.get("swap_mb")), _float(data.get("load")),
                        _int((data.get("disk") or "").rstrip("%")), data.get("xray", ""),
                    ),
                )
                for email, v in stats.items():
                    if not email.startswith("c.") or (v["up"] + v["down"]) == 0:
                        continue
                    parts = email.split(".", 2)
                    if len(parts) == 3:
                        self.db.x("INSERT INTO traffic(ts,node,chain,client,up,down) VALUES(?,?,?,?,?,?)", (now, name, parts[1], parts[2], v["up"], v["down"]))
                good += 1
            self.db.kv_set("collector.last_status", "ok" if good == total else "error")
            self.db.kv_set("collector.last_error", err)
            if int(time.time()) // 86400 != int(self.db.kv_get("collector.pruned", "0")):
                self.db.prune(int(self.settings.get("collector.retention_days")))
                self.db.kv_set("collector.pruned", str(int(time.time()) // 86400))
        except Exception as exc:  # сбор не должен валить панель
            self.db.kv_set("collector.last_status", "error")
            self.db.kv_set("collector.last_error", f"{type(exc).__name__}: {exc}"[:200])
            self.log(f"collector: {exc}")
        finally:
            self.db.kv_set("collector.last_end", str(int(time.time())))
            self._lock.release()
        return good, total

    def start(self) -> None:
        def loop():
            # первый проход вскоре после старта, дальше по расписанию из настроек
            if self._stop.wait(8):
                return
            while not self._stop.is_set():
                self.run_once()
                if self._stop.wait(self.interval()):
                    return

        self._thread = threading.Thread(target=loop, name="collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
