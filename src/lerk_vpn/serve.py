"""Мини-сервер подписок: клиентское приложение (Happ, v2rayN и др.) забирает ссылки по токену."""

from __future__ import annotations

import base64
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .model import SpecError, load_clients, load_topology
from .state import State
from .xraycfg import client_links

TOKEN_RE = re.compile(r"^/sub/([0-9a-f]{24})$")


def render_subscription(workdir: Path, token: str, plain: bool = False) -> tuple[str, str] | None:
    """Возвращает (тело, заголовок профиля) или None, если клиента нет или он отключён."""
    topo = load_topology(workdir / "topology.toml")
    clients = load_clients(workdir / "clients.json", topo)
    state = State.load(workdir / "state.json")
    name = state.client_by_token(token)
    if not name:
        return None
    client = next((c for c in clients if c.name == name and c.enabled), None)
    if not client:
        return None
    text = "\n".join(client_links(topo, state, client)) + "\n"
    body = text if plain else base64.b64encode(text.encode()).decode()
    return body, f"{topo.project}-{client.name}"


def make_handler(workdir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "lerk-vpn"

        def log_message(self, fmt, *args):  # токены не пишем в журнал
            return

        def _send(self, code: int, body: str, headers: dict | None = None):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            m = TOKEN_RE.match(url.path)
            if not m:
                return self._send(404, "not found\n")
            plain = parse_qs(url.query).get("format", [""])[0] == "plain"
            try:
                found = render_subscription(workdir, m.group(1), plain)
            except (SpecError, OSError, ValueError):
                return self._send(500, "error\n")
            if not found:
                return self._send(404, "not found\n")
            body, title = found
            self._send(200, body, {"profile-title": title, "profile-update-interval": "12"})

    return Handler


def serve(workdir: Path, host: str, port: int) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(workdir))
    print(f"подписки: http://{host}:{port}/sub/<токен клиента>  (Ctrl+C для остановки)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
