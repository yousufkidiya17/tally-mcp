"""Tests for safe XML response parsing: CDATA, entities, namespaces,
repeated elements, malformed input, and hostile DTD/entity declarations."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tally_client as tc


class TestXmlParsing(unittest.TestCase):
    def test_basic_counters(self):
        xml = ("<ENVELOPE><BODY><RESULT><CREATED>2</CREATED><ALTERED>0</ALTERED>"
               "<DELETED>0</DELETED><ERRORS>0</ERRORS></RESULT></BODY></ENVELOPE>")
        res = tc.parse_import_response(xml, "Create", "Ledger", "X")
        self.assertTrue(res.success)
        self.assertEqual(res.created, 2)

    def test_cdata_lineerror(self):
        xml = ("<ENVELOPE><RESULT><CREATED>0</CREATED><ERRORS>1</ERRORS>"
               "<LINEERROR><![CDATA[Name <Cash> already exists]]></LINEERROR>"
               "</RESULT></ENVELOPE>")
        res = tc.parse_import_response(xml, "Create", "Ledger", "X")
        self.assertFalse(res.success)
        self.assertEqual(res.line_errors, ["Name <Cash> already exists"])

    def test_entities_unescaped(self):
        xml = ("<ENVELOPE><RESULT><CREATED>1</CREATED><ERRORS>0</ERRORS>"
               "<LINEERROR>a &amp; b &lt;c&gt;</LINEERROR>"
               "</RESULT></ENVELOPE>")
        res = tc.parse_import_response(xml, "Create", "Ledger", "X")
        # line error present -> failure, but content must be intact
        self.assertFalse(res.success)
        self.assertEqual(res.line_errors, ["a & b <c>"])

    def test_repeated_lineerrors_all_kept(self):
        xml = ("<ENVELOPE><RESULT><CREATED>0</CREATED><ERRORS>2</ERRORS>"
               "<LINEERROR>first</LINEERROR><LINEERROR>second</LINEERROR>"
               "</RESULT></ENVELOPE>")
        res = tc.parse_import_response(xml, "Create", "Ledger", "X")
        self.assertEqual(res.line_errors, ["first", "second"])

    def test_self_closing_lineerror_ignored(self):
        xml = ("<ENVELOPE><RESULT><CREATED>1</CREATED><ERRORS>0</ERRORS>"
               "<LINEERROR/></RESULT></ENVELOPE>")
        res = tc.parse_import_response(xml, "Create", "Ledger", "X")
        self.assertTrue(res.success)
        self.assertEqual(res.line_errors, [])

    def test_namespaced_response(self):
        xml = ('<t:ENVELOPE xmlns:t="urn:tally"><t:RESULT>'
               '<t:CREATED>3</t:CREATED><t:ERRORS>0</t:ERRORS>'
               '</t:RESULT></t:ENVELOPE>')
        res = tc.parse_import_response(xml, "Create", "Ledger", "X")
        self.assertTrue(res.success)
        self.assertEqual(res.created, 3)

    def test_malformed_is_failure_not_success(self):
        for bad in ["<ENVELOPE><RESULT><CREATED>1</CREATED>",
                    "not xml at all", "", "   ",
                    "<HTML><BODY>proxy error page</BODY></HTML>"]:
            res = tc.parse_import_response(bad, "Create", "Ledger", "X")
            self.assertFalse(res.success, msg=f"input: {bad[:30]!r}")
            self.assertIn("FAILED", res.message)

    def test_doctype_rejected(self):
        hostile = ('<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                   '<ENVELOPE><RESULT><CREATED>1</CREATED>&xxe;'
                   '</RESULT></ENVELOPE>')
        res = tc.parse_import_response(hostile, "Create", "Ledger", "X")
        self.assertFalse(res.success)
        # and the raw helper raises
        with self.assertRaises(tc.UnsafeXmlError):
            tc._parse_xml_safe(hostile)

    def test_billion_laughs_rejected(self):
        laughs = ('<!DOCTYPE l [<!ENTITY a "xxxxxxxxxxxxxxxxxxxxxxxx">'
                  '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                  '<ENVELOPE><RESULT>&b;</RESULT></ENVELOPE>')
        with self.assertRaises(tc.UnsafeXmlError):
            tc._parse_xml_safe(laughs)

    def test_collection_names_cdata_and_entities(self):
        xml = ('<COLLECTION><NAME>Cash</NAME><NAME><![CDATA[A <B>]]></NAME>'
               '<NAME>C &amp; D</NAME><NAME>Cash</NAME></COLLECTION>')
        names = tc.parse_collection_names(xml)
        self.assertEqual(names, ["Cash", "A <B>", "C & D"])

    def test_ledger_parents(self):
        xml = ('<COLLECTION>'
               '<LEDGER NAME="Cash"><PARENT TYPE="String">Cash-in-Hand</PARENT></LEDGER>'
               '<LEDGER NAME="P &amp; Q"><PARENT>Sundry Debtors</PARENT></LEDGER>'
               '</COLLECTION>')
        pairs = tc.parse_ledger_parents(xml)
        self.assertEqual(pairs, [{"name": "Cash", "group": "Cash-in-Hand"},
                                 {"name": "P & Q", "group": "Sundry Debtors"}])

    def test_missing_counters_default_zero(self):
        xml = "<ENVELOPE><RESULT></RESULT></ENVELOPE>"
        res = tc.parse_import_response(xml, "Create", "Ledger", "X")
        self.assertFalse(res.success)  # created=0 -> not success
        self.assertEqual(res.created, 0)


if __name__ == "__main__":
    unittest.main()
