import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from lerk_vpn import ops
from lerk_vpn.model import SpecError
from lerk_vpn.tomlw import dump, load_raw, save_raw
from lerk_vpn.web import happ
from lerk_vpn.web.app import App, make_handler
from lerk_vpn.web.auth import hash_password, verify_password
from lerk_vpn.web.db import DB
from lerk_vpn.web.periods import compare, fmt_bytes, get_period
from lerk_vpn.web.settings import Settings
from lerk_vpn.web.updater import Updater
from lerk_vpn.workspace import Workspace

from lerk_vpn.cli import main as cli_main
import contextlib
import io
import os


def quiet(*argv):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return cli_main(list(argv))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        os.environ["HOME"] = str(cls.tmp / "home")
        (cls.tmp / "home").mkdir()
        cls.dir = cls.tmp / "ws"
        quiet("-C", str(cls.dir), "init")
        quiet("-C", str(cls.dir), "client", "add", "ann", "--chain", "main", "--note", "тест")
        cls.app = App(cls.dir, "admin", hash_password("correct-horse-battery"))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.opener = urllib.request.build_opener(NoRedirect)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, path, data=None, cookie=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=urllib.parse.urlencode(data).encode() if data is not None else None)
        if cookie:
            r.add_header("Cookie", cookie)
        try:
            resp = self.opener.open(r)
        except urllib.error.HTTPError as e:
            resp = e
        return resp.status, resp.headers, resp.read().decode("utf-8", "replace")

    def login(self):
        status, headers, _ = self.req("/login", {"login": "admin", "password": "correct-horse-battery"})
        self.assertEqual(status, 303)
        return headers["Set-Cookie"].split(";")[0]

    def csrf(self, cookie):
        _, _, body = self.req("/?p=clients", cookie=cookie)
        return body.split('name="_csrf" value="')[1].split('"')[0]

    def test_01_pages_need_login_and_security_headers(self):
        status, headers, body = self.req("/?p=clients")
        self.assertEqual(status, 200)
        self.assertIn('name="password"', body)
        self.assertNotIn("ann", body)
        self.assertIn("noindex", headers["X-Robots-Tag"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.req("/robots.txt")[2].split("\n")[1], "Disallow: /")

    def test_02_all_pages_render_after_login(self):
        cookie = self.login()
        for page in ("pulse", "clients", "happ", "servers", "system", "settings", "audit", "client&name=ann", "qr&name=ann", "search&q=ann"):
            status, _, body = self.req(f"/?p={page}", cookie=cookie)
            self.assertEqual(status, 200, page)
            self.assertIn("<h1>", body)
        self.assertEqual(self.req("/?p=client&name=zzz", cookie=cookie)[0], 404)

    def test_03_csrf_required_for_actions(self):
        cookie = self.login()
        self.assertEqual(self.req("/do/client_add", {"name": "bob", "chains": "main"}, cookie)[0], 403)
        token = self.csrf(cookie)
        status, _, _ = self.req("/do/client_add", {"_csrf": token, "name": "bob", "chains": "main"}, cookie)
        self.assertEqual(status, 303)
        self.assertIn("bob", self.req("/?p=clients", cookie=cookie)[2])
        self.assertEqual(self.app.db.kv_get("dirty"), "1")
        actions = [r["action"] for r in self.app.db.q("SELECT action FROM audit")]
        self.assertIn("клиент создан", actions)

    def test_04_login_rate_limit_and_same_error(self):
        app = self.app
        app.settings.save({"security.max_attempts": "3"}, "t")
        bodies = set()
        for _ in range(2):
            status, _, body = self.req("/login", {"login": "nobody", "password": "x"})
            bodies.add((status, "Неверный логин или пароль" in body))
        status, _, body = self.req("/login", {"login": "admin", "password": "wrong"})
        self.assertIn((401, True), bodies)
        status, _, body = self.req("/login", {"login": "admin", "password": "correct-horse-battery"})
        self.assertEqual(status, 429)  # после трёх промахов правильный пароль тоже ждёт паузу
        app.db.x("DELETE FROM attempts")
        app.settings.save({"security.max_attempts": "8"}, "t")

    def test_05_subscription_has_happ_headers(self):
        token = json.loads((self.dir / "state.json").read_text())["clients"]["ann"]["token"]
        status, headers, body = self.req(f"/sub/{token}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["profile-update-interval"], "12")
        self.assertTrue(headers["routing"].startswith("happ://routing/onadd/"))
        self.assertIn("upload=0; download=0", headers["subscription-userinfo"])
        import base64
        self.assertTrue(base64.b64decode(body).decode().startswith("vless://"))
        self.assertEqual(self.req("/sub/" + "0" * 24)[0], 404)
        quiet("-C", str(self.dir), "client", "disable", "ann")
        self.assertEqual(self.req(f"/sub/{token}")[0], 404)
        quiet("-C", str(self.dir), "client", "enable", "ann")

    def test_06_settings_validation_and_journal(self):
        s = Settings(self.app.db)
        changed, errors = s.save({"happ.title": "Мой VPN", "collector.interval_min": "0"}, "1.2.3.4")
        self.assertIn("happ.title", changed)
        self.assertTrue(errors)  # интервал 0 не принят
        self.assertEqual(s.get("happ.title"), "Мой VPN")
        self.assertTrue(self.app.db.q("SELECT * FROM settings_log WHERE key='happ.title'"))
        _, errors = s.save({"happ.direct_sites": "bad site;;drop table"}, "x")
        self.assertTrue(errors)
        s.save({"happ.title": "lerk-vpn"}, "1.2.3.4")  # значение по умолчанию убирается из таблицы
        self.assertIsNone(self.app.db.one("SELECT 1 FROM settings WHERE key='happ.title'"))


class UnitTests(unittest.TestCase):
    def test_password_hash(self):
        h = hash_password("secret-pass-1")
        self.assertTrue(verify_password("secret-pass-1", h))
        self.assertFalse(verify_password("other", h))
        self.assertFalse(verify_password("x", "garbage"))

    def test_periods_and_compare(self):
        now = 1_795_072_145
        p = get_period("7d", now)
        self.assertEqual(p.end, now)
        self.assertEqual(p.start % 86400, (-3 * 3600) % 86400)  # полночь по Москве
        self.assertLess(p.prev_end, p.end)
        self.assertIsNone(get_period("all", now).prev_start)
        self.assertEqual(get_period("nonsense", now).key, "7d")
        self.assertEqual(compare(150, 100)[1], "good")
        self.assertEqual(compare(150, 100, lower_better=True)[1], "bad")
        self.assertEqual(compare(None, 5), ("нет данных", "muted"))
        self.assertEqual(fmt_bytes(None), "нет данных")
        self.assertEqual(fmt_bytes(1536), "1,5 КБ")

    def test_happ_profile_and_headers(self):
        s = Settings(DB(Path(tempfile.mkdtemp()) / "p.db"))
        s.db.init()
        cfg = s.all()
        prof = happ.routing_profile(cfg, 123)
        self.assertIn("domain:ru", prof["DirectSites"])
        self.assertIn("geoip:ru", prof["DirectIp"])
        self.assertEqual(prof["LastUpdated"], "123")
        link = happ.routing_link(prof)
        import base64
        self.assertEqual(json.loads(base64.b64decode(link.rsplit("/", 1)[1]))["Name"], "lerk-vpn")
        cfg["happ.announce"] = "Привет"
        h = happ.subscription_headers(cfg, "ann", 5, 7)
        self.assertTrue(h["announce"].startswith("base64:"))
        self.assertEqual(h["subscription-userinfo"], "upload=5; download=7; total=0; expire=0")
        cfg["happ.routing_on"] = False
        self.assertEqual(happ.subscription_headers(cfg, "ann", 0, 0)["routing-enable"], "0")
        self.assertEqual(happ.add_link("https://x/sub/a"), "happ://add/https://x/sub/a")

    def test_topology_edit_operations(self):
        d = Path(tempfile.mkdtemp()) / "ws"
        quiet("-C", str(d), "init")
        ws = Workspace(d)
        raw = load_raw(ws.topology_path)
        self.assertEqual(load_raw(ws.topology_path), __import__("tomllib").loads(dump(raw)))
        raw["nodes"]["de1"] = {"host": "203.0.113.30"}
        save_raw(ws.topology_path, raw)
        created = ops.cascade_create(ws, ["fi1"], "de1")
        self.assertEqual(created, ["fi1-de1"])
        with self.assertRaises(SpecError):
            ops.cascade_create(ws, ["fi1"], "de1")  # такой уже есть
        with self.assertRaises(SpecError):
            ops.cascade_create(ws, ["fi1", "de1"], "de1")
        with self.assertRaises(SpecError):
            ops.server_delete(ws, "fi1")  # входит в каскад
        ops.client_add(ws, "ann", ["fi1-de1"])
        ops.chain_delete(ws, "main")  # клиент не был на main, остаётся на новом
        _, clients, _ = ws.load()
        self.assertEqual(clients[0].chains, ["fi1-de1"])
        changed = ops.server_edit(ws, "fi1", {"port": "8443", "ssh_port": "22", "swap_gb": "4", "mux_concurrency": "8", "sni": "dl.google.com", "fingerprint": "chrome", "flow": "vision", "block_torrents": "1"})
        self.assertEqual(changed, ["port"])
        self.assertEqual(ws.load()[0].nodes["fi1"].port, 8443)
        with self.assertRaises(SpecError):
            ops.server_edit(ws, "fi1", {"port": "22", "block_torrents": "1"})  # совпадает с SSH
        with self.assertRaises(SpecError):
            ops.server_edit(ws, "fi1", {"port": "8443", "mux": "1", "flow": "vision", "block_torrents": "1"})  # mux только без flow
        ops.server_edit(ws, "de1", {"port": "443", "mux": "1", "mux_concurrency": "16", "flow": "none", "block_torrents": "1"})
        n = ws.load()[0].nodes["de1"]
        self.assertTrue(n.mux and n.flow == "" and n.mux_concurrency == 16)
        ops.server_set_role(ws, "fi1", "entry")
        self.assertEqual(ws.load()[0].nodes["fi1"].role, "entry")
        with self.assertRaises(SpecError):
            ops.server_set_role(ws, "fi1", "boss")
        backups = list((d / "backup").glob("topology.*.toml"))
        self.assertTrue(backups)
        with self.assertRaises(SpecError):
            save_raw(ws.topology_path, {"nodes": {}, "chains": {}}, allow_empty=False)
        save_raw(ws.topology_path, {"nodes": {}, "chains": {}})  # панель стартует пустой


class UpdaterTests(unittest.TestCase):
    def sh(self, *args, cwd):
        subprocess.run(args, cwd=cwd, check=True, capture_output=True, env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

    def test_check_install_rollback(self):
        base = Path(tempfile.mkdtemp())
        origin, work, clone = base / "origin.git", base / "work", base / "clone"
        self.sh("git", "init", "-q", "--bare", "-b", "main", str(origin), cwd=base)
        self.sh("git", "clone", "-q", str(origin), str(work), cwd=base)
        (work / "a.txt").write_text("1")
        self.sh("git", "add", "-A", cwd=work)
        self.sh("git", "commit", "-q", "-m", "первая версия", cwd=work)
        self.sh("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        self.sh("git", "clone", "-q", "-b", "main", str(origin), str(clone), cwd=base)
        db = DB(base / "p.db")
        db.init()
        up = Updater(db, clone)
        up._verify = lambda: ""
        self.assertEqual(up.check()["ahead"], 0)
        (work / "a.txt").write_text("2")
        self.sh("git", "commit", "-q", "-am", "вторая версия", cwd=work)
        self.sh("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        res = up.check()
        self.assertEqual(res["ahead"], 1)
        self.assertIn("вторая версия", res["commits"][0])
        ok, msg = up.install()
        self.assertTrue(ok, msg)
        self.assertEqual((clone / "a.txt").read_text(), "2")
        ok, msg = up.rollback()
        self.assertTrue(ok, msg)
        self.assertEqual((clone / "a.txt").read_text(), "1")
        # провал проверки откатывает версию
        up._verify = lambda: "тест не прошёл"
        ok, msg = up.install()
        self.assertFalse(ok)
        self.assertIn("откатена", msg)
        self.assertEqual((clone / "a.txt").read_text(), "1")
        # локальные правки блокируют установку
        up._verify = lambda: ""
        (clone / "a.txt").write_text("локально")
        ok, msg = up.install()
        self.assertFalse(ok)
        self.assertIn("локальные правки", msg)


if __name__ == "__main__":
    unittest.main()
