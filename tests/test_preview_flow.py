"""Tests for the preview -> confirm_token -> execute flow with mocked HTTP.

No live Tally is touched: requests.post is patched for every test.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tally_client as tc
from tally_client import MutationGate, TallyClient

TEST_COMPANY = "Test Company"

CREATE_OK = ("<ENVELOPE><BODY><RESULT><CREATED>1</CREATED><ERRORS>0</ERRORS>"
             "</RESULT></BODY></ENVELOPE>")


class FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status
        self.content = text.encode("utf-8")


def make_client(**kw):
    os.environ["TALLY_COMPANY"] = TEST_COMPANY
    gate = MutationGate(ttl_seconds=600)
    return TallyClient(base_url="http://127.0.0.1:9999/", gate=gate, **kw)


class TestPreviewFlow(unittest.TestCase):
    def setUp(self):
        os.environ["TALLY_COMPANY"] = TEST_COMPANY

    def tearDown(self):
        os.environ.pop("TALLY_COMPANY", None)

    def test_preview_makes_no_http_request(self):
        c = make_client()
        with patch.object(tc.requests, "post") as mock_post:
            res = c.create_ledger("Cash", "Cash-in-Hand", preview=True)
        mock_post.assert_not_called()
        self.assertTrue(res.preview)
        self.assertTrue(res.success)
        self.assertTrue(res.confirm_token)
        self.assertIn("<SVCURRENTCOMPANY>Test Company</SVCURRENTCOMPANY>", res.xml)
        self.assertEqual(res.company, TEST_COMPANY)

    def test_execute_without_token_refused_no_http(self):
        c = make_client()
        with patch.object(tc.requests, "post") as mock_post:
            res = c.create_ledger("Cash", "Cash-in-Hand",
                                  preview=False, confirm_token="")
        mock_post.assert_not_called()
        self.assertFalse(res.success)
        self.assertIn("confirm_token", res.message)

    def test_execute_with_valid_token_dispatches_once(self):
        c = make_client()
        with patch.object(tc.requests, "post",
                          return_value=FakeResp(CREATE_OK)) as mock_post:
            prev = c.create_ledger("Cash", "Cash-in-Hand", preview=True)
            res = c.create_ledger("Cash", "Cash-in-Hand",
                                  preview=False,
                                  confirm_token=prev.confirm_token)
        self.assertEqual(mock_post.call_count, 1)
        self.assertTrue(res.success)
        self.assertEqual(res.created, 1)

    def test_reused_token_second_dispatch_refused(self):
        c = make_client()
        with patch.object(tc.requests, "post",
                          return_value=FakeResp(CREATE_OK)) as mock_post:
            prev = c.create_ledger("Cash", "Cash-in-Hand", preview=True)
            r1 = c.create_ledger("Cash", "Cash-in-Hand", preview=False,
                                 confirm_token=prev.confirm_token)
            r2 = c.create_ledger("Cash", "Cash-in-Hand", preview=False,
                                 confirm_token=prev.confirm_token)
        self.assertTrue(r1.success)
        self.assertFalse(r2.success)
        self.assertEqual(mock_post.call_count, 1)  # at most one dispatch

    def test_changed_args_invalidate_token(self):
        c = make_client()
        with patch.object(tc.requests, "post",
                          return_value=FakeResp(CREATE_OK)) as mock_post:
            prev = c.create_ledger("Cash", "Cash-in-Hand", preview=True)
            # different name -> rebuilt payload hash differs
            res = c.create_ledger("Bank", "Bank Accounts", preview=False,
                                  confirm_token=prev.confirm_token)
        mock_post.assert_not_called()
        self.assertFalse(res.success)

    def test_missing_company_fails_closed_no_http(self):
        os.environ.pop("TALLY_COMPANY", None)
        c = TallyClient(base_url="http://127.0.0.1:9999/",
                        gate=MutationGate())
        with patch.object(tc.requests, "post") as mock_post:
            res = c.create_ledger("Cash", "Cash-in-Hand", preview=True)
        mock_post.assert_not_called()
        self.assertFalse(res.success)
        self.assertIn("TALLY_COMPANY", res.message)

    def test_whitespace_company_fails_closed(self):
        os.environ["TALLY_COMPANY"] = "   "
        c = TallyClient(base_url="http://127.0.0.1:9999/",
                        gate=MutationGate())
        with patch.object(tc.requests, "post") as mock_post:
            res = c.create_ledger("Cash", "Cash-in-Hand", preview=True)
        mock_post.assert_not_called()
        self.assertFalse(res.success)

    def test_transport_failure_no_retry(self):
        import requests as _rq
        c = make_client()
        with patch.object(tc.requests, "post",
                          side_effect=_rq.ConnectionError("down")) as mock_post:
            prev = c.create_ledger("Cash", "Cash-in-Hand", preview=True)
            res = c.create_ledger("Cash", "Cash-in-Hand", preview=False,
                                  confirm_token=prev.confirm_token)
        self.assertEqual(mock_post.call_count, 1)  # exactly one attempt
        self.assertFalse(res.success)
        self.assertIn("No automatic retry", res.message)

    def test_tally_error_response_is_failure(self):
        err_xml = ("<ENVELOPE><BODY><RESULT><CREATED>0</CREATED>"
                   "<ERRORS>1</ERRORS><LINEERROR>Duplicate name</LINEERROR>"
                   "</RESULT></BODY></ENVELOPE>")
        c = make_client()
        with patch.object(tc.requests, "post",
                          return_value=FakeResp(err_xml)):
            prev = c.create_ledger("Cash", "Cash-in-Hand", preview=True)
            res = c.create_ledger("Cash", "Cash-in-Hand", preview=False,
                                  confirm_token=prev.confirm_token)
        self.assertFalse(res.success)
        self.assertIn("Duplicate name", res.message)


if __name__ == "__main__":
    unittest.main()
