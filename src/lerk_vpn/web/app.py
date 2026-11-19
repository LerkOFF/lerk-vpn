"""HTTP-приложение панели: вход, страницы, действия, выдача подписок для Happ."""

from __future__ import annotations

import base64
import json
import logging
import logging.handlers
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..model import SpecError
from ..remote import RemoteError
from ..state import State
from ..workspace import Workspace
from ..xraycfg import client_links
from . import actions, happ, ui, views
from .auth import COOKIE, Auth
from .collector import Collector
from .db import DB
from .geo import Geo
from .jobs import Jobs
from .settings import Settings
from .updater import Updater

STATIC = Path(__file__).parent / "static"
FLASH = "lvf"
MAX_BODY = 64 * 1024
SUB_RE = re.compile(r"^/sub/([0-9a-f]{24})$")
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
log = logging.getLogger("lerk-vpn")


class App:
    def __init__(self, workdir: Path, login: str, password_hash: str, secure: bool = False):
        self.ws = Workspace(workdir, allow_empty=True)
        if not self.ws.topology_path.exists():
            self.ws.topology_path.write_text('project = "main"\n', 'utf-8')
        if not self.ws.clients_path.exists():
            self.ws.clients_path.write_text('{"clients": []}\n', 'utf-8')
        self.ws.root.mkdir(parents=True, exist_ok=True)
        self.db = DB(workdir / "panel.db")
        self.db.init()
        self.geo = Geo(workdir / "geo")
        self.settings = Settings(self.db, self.geo)
        self.auth = Auth(login, password_hash, self.db, self.settings)
        self.collector = Collector(self.ws, self.db, self.settings, log=log.warning)
        self.jobs = Jobs(self.db)
        self.updater = Updater(self.db)
        self.secure = secure
        self.pubkey = ensure_ssh_key()

    def start_background(self) -> None:
        self.jobs.mark_stale()
        self.collector.start()
        threading.Thread(target=self._update_loop, daemon=True, name="update-check").start()

    def geo_urls(self) -> dict:
        s = self.settings.all()
        return {"geoip": s["happ.geoip_url"], "geosite": s["happ.geosite_url"]}

    def _update_loop(self) -> None:
        time.sleep(30)
        while True:
            try:
                # geo-базы: при первом запуске и раз в неделю
                age = time.time() - min((p.stat().st_mtime for p in (self.geo.path("geoip"), self.geo.path("geosite")) if p.exists()), default=0)
                if not self.geo.loaded() or (self.settings.get("happ.geo_auto") and age > 7 * 86400):
                    if self.geo.download(self.geo_urls(), log=log.info):
                        self.db.kv_set("happ.routing_updated", str(int(time.time())))
            except Exception as exc:
                log.warning("geo-базы: %s", exc)
            try:
                if self.settings.get("updates.auto_check") and time.time() - int(self.db.kv_get("update.checked", "0") or 0) > 86400:
                    self.updater.check()
            except Exception as exc:
                log.warning("проверка обновлений: %s", exc)
            time.sleep(3600)


def ensure_ssh_key() -> Path:
    """Своя пара SSH-ключей панели: публичную часть кладём на добавляемые серверы."""
    ssh = Path.home() / ".ssh"
    ssh.mkdir(mode=0o700, exist_ok=True)
    key = ssh / "id_ed25519"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "lerk-vpn-panel", "-f", str(key)], check=True)
    return ssh / "id_ed25519.pub"


class Ctx:
    def __init__(self, app: App, h: BaseHTTPRequestHandler, q: dict, forms: dict):
        self.app, self.db, self.ws, self.settings = app, app.db, app.ws, app.settings
        self.q = {k: v[0] for k, v in q.items()}
        self.forms = forms
        self.form = {k: v[0] for k, v in forms.items()}
        self.h = h
        self.ip = client_ip(h)
        self.flash: tuple[str, str] | None = None
        self.csrf = ""
        self.session = ""
        self.actor = "admin"
        self.base_url = base_url(app, h)

    def audit(self, action: str, target: str = "", detail: str = "") -> None:
        self.db.audit(self.actor, self.ip, action, target, detail)

    def referer_page(self, default: str) -> str:
        ref = self.h.headers.get("Referer", "")
        page = parse_qs(urlparse(ref).query).get("p", [""])[0]
        return ui.url(page) if page in views.PAGES and page not in ("client", "job", "qr") else ui.url(default)


def trusted_proxy(h) -> bool:
    return h.client_address[0] in ("127.0.0.1", "::1")


def client_ip(h) -> str:
    if trusted_proxy(h):
        real = h.headers.get("X-Real-IP") or (h.headers.get("X-Forwarded-For", "").split(",")[0].strip())
        if real:
            return real[:45]
    return h.client_address[0]


def base_url(app: App, h) -> str:
    configured = str(app.settings.get("panel.public_url") or "").rstrip("/")
    if configured:
        return configured
    proto = h.headers.get("X-Forwarded-Proto", "http") if trusted_proxy(h) else "http"
    host = re.sub(r"[^A-Za-z0-9.:\[\]-]", "", h.headers.get("Host", "localhost"))
    return f"{proto}://{host}"


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "lerk-vpn"
        sys_version = ""

        def log_message(self, fmt, *args):
            line = fmt % args
            log.info("%s %s", self.client_address[0], re.sub(r"/sub/[0-9a-f]{24}", "/sub/***", line))

        # ---------------------------------------------------------------- отправка
        def _headers(self, code: int, ctype: str, length: int, extra: dict | None = None, cookies: list[str] | None = None, cache: str = "no-store"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            for c in cookies or []:
                self.send_header("Set-Cookie", c)
            self.end_headers()

        def send_html(self, code: int, body: str, cookies=None):
            data = body.encode()
            self._headers(code, "text/html; charset=utf-8", len(data), cookies=cookies)
            if self.command != "HEAD":
                self.wfile.write(data)

        def send_text(self, code: int, body: str, ctype: str = "text/plain; charset=utf-8", extra=None):
            data = body.encode()
            self._headers(code, ctype, len(data), extra)
            if self.command != "HEAD":
                self.wfile.write(data)

        def redirect(self, to: str, cookies=None):
            self._headers(303, "text/plain", 0, {"Location": to}, cookies)

        def cookie_attrs(self, max_age: int | None = None) -> str:
            secure = app.secure or (trusted_proxy(self) and self.headers.get("X-Forwarded-Proto") == "https")
            a = "; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if secure else "")
            return a + (f"; Max-Age={max_age}" if max_age is not None else "")

        def read_cookie(self, name: str) -> str | None:
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            c = SimpleCookie()
            try:
                c.load(raw)
            except Exception:
                return None
            return c[name].value if name in c else None

        # ---------------------------------------------------------------- маршруты
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                self._get()
            except views.NotFound:
                self.send_html(404, "<h1>Не найдено</h1>")
            except BrokenPipeError:
                pass
            except Exception:
                log.exception("GET %s", self.path.split("?")[0])
                self.send_html(500, "<h1>Ошибка</h1><p>Подробности в журнале панели.</p>")

        def do_POST(self):
            try:
                self._post()
            except BrokenPipeError:
                pass
            except Exception:
                log.exception("POST %s", self.path.split("?")[0])
                self.send_html(500, "<h1>Ошибка</h1><p>Подробности в журнале панели.</p>")

        def _get(self):
            u = urlparse(self.path)
            path = u.path
            if path == "/health":
                return self.send_text(200, json.dumps({"ok": True}), "application/json")
            if path == "/robots.txt":
                return self.send_text(200, "User-agent: *\nDisallow: /\n")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            m = SUB_RE.match(path)
            if m:
                return self._subscription(m.group(1), parse_qs(u.query).get("format", [""])[0] == "plain")
            if not app.auth.enabled:
                return self.send_html(503, ui.disabled_page())
            ctx = self._ctx(parse_qs(u.query, keep_blank_values=True), {})
            if not ctx:
                return
            page = ctx.q.get("p", "pulse")
            if page == "job" and ctx.q.get("raw"):
                return self.send_text(200, views.job_json(ctx), "application/json")
            fn = views.PAGES.get(page)
            if not fn:
                raise views.NotFound()
            body = fn(ctx)
            self.send_html(200, body, cookies=[f"{FLASH}=; Max-Age=0{self.cookie_attrs()}"] if self.read_cookie(FLASH) else None)

        def _post(self):
            u = urlparse(self.path)
            if not app.auth.enabled:
                return self.send_html(503, ui.disabled_page())
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > MAX_BODY:
                return self.send_text(413, "слишком большой запрос")
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            forms = parse_qs(raw, keep_blank_values=True)
            ip = client_ip(self)
            if u.path == "/login":
                return self._login(forms, ip)
            ctx = self._ctx({}, forms, need_login_page=False)
            if not ctx:
                return self.redirect("/")
            if not app.auth.csrf_ok(ctx.session, ctx.form.get("_csrf")):
                return self.send_text(403, "неверный токен формы, обновите страницу")
            if u.path == "/logout":
                ctx.audit("выход")
                return self.redirect("/", cookies=[f"{COOKIE}=; Max-Age=0{self.cookie_attrs()}"])
            if not u.path.startswith("/do/"):
                return self.send_text(404, "не найдено")
            fn = actions.ACTIONS.get(u.path[4:])
            if not fn:
                return self.send_text(404, "нет такого действия")
            try:
                kind, text, to = fn(ctx)
            except (SpecError, RemoteError, ValueError) as exc:
                kind, text, to = "err", str(exc), ctx.referer_page("pulse")
            payload = base64.urlsafe_b64encode(json.dumps([kind, text], ensure_ascii=False).encode()).decode()
            self.redirect(to, cookies=[f"{FLASH}={payload}{self.cookie_attrs(60)}"])

        def _ctx(self, q, forms, need_login_page: bool = True):
            ctx = Ctx(app, self, q, forms)
            session = app.auth.check_cookie(self.read_cookie(COOKIE))
            if not session:
                if need_login_page:
                    if self.command == "GET" and urlparse(self.path).path != "/":
                        self.redirect("/")
                    else:
                        self.send_html(200, ui.login_page(brand=str(app.settings.get("panel.brand")), blocked=app.auth.blocked_for(ctx.ip)))
                return None
            ctx.session, ctx.csrf = session, app.auth.csrf(session)
            raw = self.read_cookie(FLASH)
            if raw:
                try:
                    kind, text = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
                    ctx.flash = (kind if kind in ("ok", "err", "info") else "info", str(text))
                except Exception:
                    pass
            return ctx

        def _login(self, forms, ip):
            brand = str(app.settings.get("panel.brand"))
            wait = app.auth.blocked_for(ip)
            if wait:
                return self.send_html(429, ui.login_page(blocked=wait, brand=brand))
            login = forms.get("login", [""])[0]
            password = forms.get("password", [""])[0]
            if app.auth.try_login(login, password):
                app.auth.success(ip)
                app.db.audit("admin", ip, "вход")
                return self.redirect("/", cookies=[f"{COOKIE}={app.auth.make_cookie()}{self.cookie_attrs(int(app.settings.get('security.session_days')) * 86400)}"])
            app.auth.fail(ip)
            app.db.audit("-", ip, "неверный пароль")
            wait = app.auth.blocked_for(ip)
            self.send_html(401 if not wait else 429, ui.login_page(error="Неверный логин или пароль", blocked=wait, brand=brand))

        def _static(self, name: str):
            f = (STATIC / name).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self.send_text(404, "не найдено")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            data = f.read_bytes()
            self._headers(200, ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""), len(data), cache="public, max-age=300")
            if self.command != "HEAD":
                self.wfile.write(data)

        def _subscription(self, token: str, plain: bool):
            try:
                topo, clients, state = app.ws.load()
                name = state.client_by_token(token)
                client = next((c for c in clients if c.name == name and c.enabled), None) if name else None
                if not client:
                    return self.send_text(404, "not found\n")
                s = app.settings.all()
                row = app.db.one("SELECT COALESCE(SUM(up),0) AS up, COALESCE(SUM(down),0) AS down FROM traffic WHERE client=?", (client.name,))
                updated = int(app.db.kv_get("happ.routing_updated", "0") or 0) or None
                headers = happ.subscription_headers(s, client.name, int(row["up"]), int(row["down"]), updated, app.geo)
                text = "\n".join(client_links(topo, state, client)) + "\n"
                body = text if plain else base64.b64encode(text.encode()).decode()
                self.send_text(200, body, "text/plain; charset=utf-8", headers)
            except (SpecError, OSError, ValueError, KeyError):
                self.send_text(500, "error\n")

    return Handler


def configure_logging(workdir: Path) -> None:
    handler = logging.handlers.RotatingFileHandler(workdir / "panel.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.addHandler(logging.StreamHandler(sys.stderr))
    log.setLevel(logging.INFO)
    try:
        (workdir / "panel.log").chmod(0o600)
    except OSError:
        pass


def run(workdir: Path, host: str, port: int) -> int:
    login = os.environ.get("LERK_VPN_ADMIN_LOGIN", "admin")
    pw_hash = os.environ.get("LERK_VPN_ADMIN_HASH", "")
    workdir.mkdir(parents=True, exist_ok=True)
    workdir.chmod(0o700)
    configure_logging(workdir)
    app = App(workdir, login, pw_hash, secure=os.environ.get("LERK_VPN_SECURE") == "1")
    if not pw_hash:
        log.warning("хеш пароля не задан (LERK_VPN_ADMIN_HASH): панель выключена, отвечает 503")
    app.start_background()
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    log.info("панель на http://%s:%s, рабочая папка %s", host, port, workdir)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.collector.stop()
        httpd.server_close()
    return 0
