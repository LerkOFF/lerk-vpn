import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from lerk_vpn.cli import main
from lerk_vpn.serve import render_subscription


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_full_offline_flow(self):
        d = ["-C", self.dir]
        self.assertEqual(run(*d, "init")[0], 0)
        code, out, _ = run(*d, "plan")
        self.assertEqual(code, 0)
        self.assertIn("fi1 -> fr1", out)
        code, out, _ = run(*d, "client", "add", "ann", "--chain", "main", "--note", "тест")
        self.assertEqual(code, 0)
        self.assertIn("vless://", out)
        self.assertEqual(run(*d, "client", "add", "ann", "--chain", "main")[0], 2)
        self.assertEqual(run(*d, "client", "add", "x", "--chain", "nope")[0], 2)
        code, out, _ = run(*d, "links", "ann", "--plain")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("vless://"))
        code, out, _ = run(*d, "plan", "--show-config", "fr1")
        self.assertIn("h.main.0", out)
        self.assertEqual(run(*d, "apply", "--dry-run")[0], 0)
        self.assertEqual(run(*d, "export")[0], 0)
        self.assertTrue((Path(self.dir) / "out/subscriptions/ann.txt").exists())
        state_mode = (Path(self.dir) / "state.json").stat().st_mode & 0o777
        self.assertEqual(state_mode, 0o600)

    def test_disable_and_subscription(self):
        d = ["-C", self.dir]
        run(*d, "init")
        run(*d, "client", "add", "ann", "--chain", "main")
        import json

        token = json.loads((Path(self.dir) / "state.json").read_text())["clients"]["ann"]["token"]
        body, title = render_subscription(Path(self.dir), token)
        self.assertEqual(title, "main-ann")
        self.assertTrue(body)
        plain, _ = render_subscription(Path(self.dir), token, plain=True)
        self.assertTrue(plain.startswith("vless://"))
        run(*d, "client", "disable", "ann")
        self.assertIsNone(render_subscription(Path(self.dir), token))
        self.assertIsNone(render_subscription(Path(self.dir), "0" * 24))

    def test_rotate_changes_uuid_and_token(self):
        d = ["-C", self.dir]
        run(*d, "init")
        run(*d, "client", "add", "ann", "--chain", "main")
        _, before, _ = run(*d, "links", "ann", "--plain")
        run(*d, "client", "rotate", "ann")
        _, after, _ = run(*d, "links", "ann", "--plain")
        self.assertNotEqual(before, after)
