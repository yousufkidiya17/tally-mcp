"""Tests for PDF export path safety: traversal, absolute paths,
Windows separators, control characters, collisions, and the happy path."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


class TestPdfPath(unittest.TestCase):
    def test_normal_voucher_number(self):
        p = server._resolve_invoice_path("SALE-001")
        root = os.path.realpath(server._INVOICE_DIR)
        self.assertTrue(p.startswith(root))
        self.assertTrue(p.endswith("invoice_SALE-001.pdf"))

    def test_traversal_neutralized(self):
        for evil in ["../../etc/passwd", "..\\..\\win.ini", "/etc/passwd",
                     "C:\\Windows\\x.pdf", "a/b\\c", "....//....//x"]:
            p = server._resolve_invoice_path(evil)
            root = os.path.realpath(server._INVOICE_DIR)
            self.assertEqual(os.path.commonpath([root, os.path.realpath(p)]),
                             root, msg=evil)
            self.assertNotIn("..", os.path.basename(p))

    def test_control_characters_sanitized(self):
        p = server._resolve_invoice_path("inv\x001\n2")
        self.assertNotIn("\x00", p)
        self.assertNotIn("\n", p)

    def test_empty_and_dot_names_rejected(self):
        for bad in ["", "   ", ".", ".."]:
            with self.assertRaises(ValueError, msg=f"{bad!r}"):
                server._safe_invoice_filename(bad)

    def test_deterministic_mapping(self):
        a = server._safe_invoice_filename("SALE-001")
        b = server._safe_invoice_filename("SALE-001")
        self.assertEqual(a, b)

    def test_collision_gets_suffix(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.realpath
            # point the invoice dir at the temp dir via monkeypatching
            orig = server._INVOICE_DIR
            server._INVOICE_DIR = tmp
            try:
                p1 = server._resolve_invoice_path("X-1")
                open(p1, "w").write("x")
                p2 = server._resolve_invoice_path("X-1")
                self.assertNotEqual(p1, p2)
                self.assertTrue(p2.endswith("-2.pdf"))
            finally:
                server._INVOICE_DIR = orig

    def test_symlink_escape_contained(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "outside")
            os.makedirs(outside)
            linkdir = os.path.join(tmp, "linkdir")
            try:
                os.symlink(outside, linkdir)
            except OSError as e:
                self.skipTest(f"Symlinks not permitted in this environment: {e}")
            orig = server._INVOICE_DIR
            server._INVOICE_DIR = linkdir
            try:
                p = server._resolve_invoice_path("S-1")
                # resolves inside the real target dir, still contained
                root = os.path.realpath(linkdir)
                self.assertEqual(os.path.commonpath([root, os.path.realpath(p)]),
                                 root)
            finally:
                server._INVOICE_DIR = orig

    def test_portable_dir_not_hardcoded(self):
        # Built from the user's home dir, not a hardcoded absolute path.
        self.assertEqual(server._INVOICE_DIR,
                         os.path.join(os.path.expanduser("~"),
                                      "tally_invoices"))


if __name__ == "__main__":
    unittest.main()
