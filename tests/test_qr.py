import hashlib
import unittest

from lerk_vpn.web import qr


def digest(m):
    return hashlib.sha256("\n".join("".join("1" if v else "0" for v in row) for row in m).encode()).hexdigest()[:16]


class QrTests(unittest.TestCase):
    def test_sizes_and_finders(self):
        m = qr.make("hello", "M")
        self.assertEqual(len(m), 21)  # версия 1
        for r0, c0 in ((0, 0), (0, 14), (14, 0)):
            self.assertTrue(m[r0][c0] and m[r0 + 6][c0 + 6] and not m[r0 + 1][c0 + 1] and m[r0 + 3][c0 + 3])
        self.assertTrue(m[13][8])  # тёмный модуль

    def test_grows_with_data(self):
        small = len(qr.make("x" * 10, "M"))
        big = len(qr.make("x" * 250, "M"))
        self.assertGreater(big, small)
        with self.assertRaises(ValueError):
            qr.make("x" * 5000, "M")

    def test_matches_reference_matrix(self):
        # эталон снят с библиотеки qrcode для маски 0 и версий 3 и 9
        self.assertEqual(digest(qr.make("https://vpn.example.com/sub/0123456789abcdef01234567", "M", mask=0)), REF_SUB)
        self.assertEqual(digest(qr.make("a" * 150, "L", mask=0)), REF_LONG)

    def test_svg(self):
        s = qr.svg("https://example.com")
        self.assertTrue(s.startswith("<svg") and "<path" in s and "viewBox" in s)


REF_SUB = "b1ec020e98f7615a"
REF_LONG = "95c79847fb5f843d"
