"""Tests for deletion target validation with mocked Tally exports.

Covers: exact match found, missing target, ambiguous target, non-empty
group refused, target changed between preview and execute, and the
voucher day-book verification path.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tally_client as tc
from tally_client import MutationGate, TallyClient

TEST_COMPANY = "Test Company"

LEDGERS_XML = ("<ENVELOPE><COLLECTION>"
               "<LEDGER NAME=\"Cash\"><PARENT>Cash-in-Hand</PARENT></LEDGER>"
               "<LEDGER NAME=\"Party\"><PARENT>Sundry Debtors</PARENT></LEDGER>"
               "</COLLECTION></ENVELOPE>")
GROUPS_XML = ("<ENVELOPE><COLLECTION>"
              "<GROUP NAME=\"Cash-in-Hand\"><PARENT>Primary</PARENT></GROUP>"
              "<GROUP NAME=\"Empty Group\"><PARENT>Primary</PARENT></GROUP>"
              "</COLLECTION></ENVELOPE>")
DAYBOOK_XML = ("<ENVELOPE><BODY>"
               "<VOUCHER VCHTYPE=\"Purchase\" REMOTEID=\"TXN-PUR-1\">"
               "<DATE>20260401</DATE><VOUCHERNUMBER>5</VOUCHERNUMBER>"
               "</VOUCHER>"
               "<VOUCHER VCHTYPE=\"Sales\" REMOTEID=\"TXN-SAL-9\">"
               "<DATE>20260401</DATE></VOUCHER>"
               "</BODY></ENVELOPE>")
DELETE_OK = ("<ENVELOPE><BODY><RESULT><DELETED>1</DELETED><ERRORS>0</ERRORS>"
             "</RESULT></BODY></ENVELOPE>")


class FakeResp:
    def __init__(self, text):
        self.text = text
        self.status_code = 200
        self.content = text.encode("utf-8")


def make_client():
    os.environ["TALLY_COMPANY"] = TEST_COMPANY
    return TallyClient(base_url="http://127.0.0.1:9999/",
                       gate=MutationGate(ttl_seconds=600))


def route_post(body_state):
    """Return a fake post() that serves canned exports and records imports."""
    def fake_post(url, data=None, headers=None, timeout=None):
        body = data.decode("utf-8") if isinstance(data, bytes) else data
        if "TALLYREQUEST>Import Data" in body:
            body_state["imports"].append(body)
            return FakeResp(DELETE_OK)
        if "ListLedger" in body:
            return FakeResp(LEDGERS_XML)
        if "ListGroup" in body:
            return FakeResp(GROUPS_XML)
        if "Day Book" in body:
            return FakeResp(DAYBOOK_XML)
        return FakeResp("<ENVELOPE/>")
    return fake_post


class TestDeleteValidation(unittest.TestCase):
    def setUp(self):
        os.environ["TALLY_COMPANY"] = TEST_COMPANY
        self.state = {"imports": []}

    def tearDown(self):
        os.environ.pop("TALLY_COMPANY", None)

    def test_delete_missing_target_refused_no_http_import(self):
        c = make_client()
        with patch.object(tc.requests, "post",
                          side_effect=route_post(self.state)):
            res = c.delete_ledger("Ghost Ledger", preview=True)
        self.assertFalse(res.success)
        self.assertIn("not found", res.message)
        self.assertEqual(self.state["imports"], [])

    def test_delete_found_preview_then_execute(self):
        c = make_client()
        with patch.object(tc.requests, "post",
                          side_effect=route_post(self.state)):
            prev = c.delete_ledger("Cash", preview=True)
            self.assertTrue(prev.success)
            self.assertTrue(prev.confirm_token)
            self.assertIn("Cash", prev.message)
            res = c.delete_ledger("Cash", preview=False,
                                  confirm_token=prev.confirm_token)
        self.assertTrue(res.success)
        self.assertEqual(len(self.state["imports"]), 1)
        self.assertIn("Test Company", self.state["imports"][0])

    def test_delete_execute_without_token_refused(self):
        c = make_client()
        with patch.object(tc.requests, "post",
                          side_effect=route_post(self.state)):
            res = c.delete_ledger("Cash", preview=False, confirm_token="")
        self.assertFalse(res.success)
        self.assertEqual(self.state["imports"], [])

    def test_delete_nonempty_group_refused(self):
        c = make_client()
        with patch.object(tc.requests, "post",
                          side_effect=route_post(self.state)):
            res = c.delete_group("Cash-in-Hand", preview=True)
        self.assertFalse(res.success)
        self.assertIn("not empty", res.message)
        self.assertEqual(self.state["imports"], [])

    def test_delete_empty_group_ok(self):
        c = make_client()
        with patch.object(tc.requests, "post",
                          side_effect=route_post(self.state)):
            prev = c.delete_group("Empty Group", preview=True)
            self.assertTrue(prev.success, prev.message)
            res = c.delete_group("Empty Group", preview=False,
                                 confirm_token=prev.confirm_token)
        self.assertTrue(res.success)
        self.assertEqual(len(self.state["imports"]), 1)

    def test_delete_target_changed_after_preview(self):
        c = make_client()
        calls = {"n": 0}
        def flip_post(url, data=None, headers=None, timeout=None):
            body = data.decode("utf-8") if isinstance(data, bytes) else data
            if "TALLYREQUEST>Import Data" in body:
                self.state["imports"].append(body)
                return FakeResp(DELETE_OK)
            if "ListLedger" in body:
                calls["n"] += 1
                # second validation: target vanished
                if calls["n"] > 1:
                    return FakeResp("<ENVELOPE><COLLECTION></COLLECTION></ENVELOPE>")
                return FakeResp(LEDGERS_XML)
            return FakeResp("<ENVELOPE/>")
        with patch.object(tc.requests, "post", side_effect=flip_post):
            prev = c.delete_ledger("Cash", preview=True)
            self.assertTrue(prev.success)
            res = c.delete_ledger("Cash", preview=False,
                                  confirm_token=prev.confirm_token)
        self.assertFalse(res.success)
        # fail-closed: the execute-time fresh validation sees the target gone
        self.assertIn("not found", res.message)
        self.assertEqual(self.state["imports"], [])

    def test_voucher_delete_requires_date(self):
        c = make_client()
        with patch.object(tc.requests, "post",
                          side_effect=route_post(self.state)):
            res = c.delete_voucher("Purchase", remote_id="TXN-PUR-1",
                                   preview=True)
        self.assertFalse(res.success)
        self.assertIn("date", res.message.lower())
        self.assertEqual(self.state["imports"], [])

    def test_voucher_delete_verified_then_executed(self):
        c = make_client()
        with patch.object(tc.requests, "post",
                          side_effect=route_post(self.state)):
            prev = c.delete_voucher("Purchase", remote_id="TXN-PUR-1",
                                    date="20260401", preview=True)
            self.assertTrue(prev.success, prev.message)
            res = c.delete_voucher("Purchase", remote_id="TXN-PUR-1",
                                   date="20260401", preview=False,
                                   confirm_token=prev.confirm_token)
        self.assertTrue(res.success)
        self.assertEqual(len(self.state["imports"]), 1)
        self.assertIn("Vouchers", self.state["imports"][0])

    def test_voucher_delete_wrong_type_refused(self):
        c = make_client()
        with patch.object(tc.requests, "post",
                          side_effect=route_post(self.state)):
            res = c.delete_voucher("Sales", remote_id="TXN-PUR-1",
                                   date="20260401", preview=True)
        self.assertFalse(res.success)
        self.assertIn("Purchase", res.message)
        self.assertEqual(self.state["imports"], [])

    def test_voucher_delete_unknown_remoteid_refused(self):
        c = make_client()
        with patch.object(tc.requests, "post",
                          side_effect=route_post(self.state)):
            res = c.delete_voucher("Purchase", remote_id="NOPE",
                                   date="20260401", preview=True)
        self.assertFalse(res.success)
        self.assertIn("no voucher", res.message.lower())
        self.assertEqual(self.state["imports"], [])


if __name__ == "__main__":
    unittest.main()
