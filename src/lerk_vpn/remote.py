"""Работа с серверами через системный ssh (учитывает ~/.ssh/config, ключи и ProxyCommand)."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .model import Node


class RemoteError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, output: str = ""):
        super().__init__(message)
        self.code = code
        self.output = output


@dataclass
class Result:
    code: int
    out: str
    err: str


SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=20",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
]


def ssh_target(node: Node) -> list[str]:
    if node.ssh_alias:
        return [node.ssh_alias]
    return ["-p", str(node.ssh_port), "-l", node.ssh_user, node.host]


class Remote:
    def __init__(self, node: Node):
        self.node = node

    def _base(self, batch: bool = True) -> list[str]:
        cmd = ["ssh", *SSH_OPTS]
        if batch:
            cmd += ["-o", "BatchMode=yes"]
        return cmd + ssh_target(self.node)

    def run(self, script: str, timeout: int = 600, check: bool = True) -> Result:
        """Выполняет bash-скрипт на сервере (скрипт идёт через stdin, в аргументы ничего не подставляется)."""
        proc = subprocess.run(
            self._base() + ["bash", "-s"],
            input=script, capture_output=True, text=True, timeout=timeout,
        )
        res = Result(proc.returncode, proc.stdout, proc.stderr)
        if check and proc.returncode != 0:
            raise RemoteError(f"{self.node.name}: команда завершилась с кодом {proc.returncode}", proc.returncode, (proc.stdout + proc.stderr).strip())
        return res

    def put(self, content: str, path: str, mode: str = "600", timeout: int = 60) -> None:
        script = f"set -e\numask 077\ncat > {shlex.quote(path)}\nchmod {int(mode, 8):o} {shlex.quote(path)}\n"
        proc = subprocess.run(self._base() + ["bash", "-c", script], input=content, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise RemoteError(f"{self.node.name}: не удалось записать {path}", proc.returncode, proc.stderr.strip())

    def reachable(self) -> tuple[bool, str]:
        try:
            res = self.run("echo ok", timeout=40, check=False)
        except subprocess.TimeoutExpired:
            return False, "таймаут"
        if res.code == 0 and res.out.strip() == "ok":
            return True, ""
        return False, (res.err.strip().splitlines() or ["нет ответа"])[-1]


def install_key(node: Node, password: str, pubkey_path: Path) -> None:
    """Кладёт публичный ключ на сервер, входя по паролю. Пароль идёт только в окружение процесса ssh."""
    pub = pubkey_path.read_text("utf-8").strip()
    if not pub.startswith(("ssh-", "ecdsa-", "sk-")):
        raise RemoteError(f"{pubkey_path}: это не публичный ключ")
    with tempfile.TemporaryDirectory(prefix="lerk-vpn-") as tmp:
        helper = Path(tmp) / "askpass.sh"
        helper.write_text('#!/bin/sh\nprintf "%s\\n" "$LERK_VPN_PASSWORD"\n')
        helper.chmod(stat.S_IRWXU)
        env = {**os.environ, "SSH_ASKPASS": str(helper), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": os.environ.get("DISPLAY", ":0"), "LERK_VPN_PASSWORD": password}
        # ключ добавляется только если его там ещё нет
        script = (
            "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
            "k=$(cat); grep -qxF \"$k\" ~/.ssh/authorized_keys || printf '%s\\n' \"$k\" >> ~/.ssh/authorized_keys"
        )
        cmd = ["ssh", *SSH_OPTS, "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "NumberOfPasswordPrompts=1"]
        cmd += ssh_target(node) + ["sh", "-c", shlex.quote(script)]
        proc = subprocess.run(cmd, input=pub + "\n", capture_output=True, text=True, env=env, timeout=90)
        if proc.returncode != 0:
            raise RemoteError(f"{node.name}: не удалось войти по паролю", proc.returncode, proc.stderr.strip())
