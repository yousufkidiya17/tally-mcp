"""Tests for the MutationGate: issue/consume, expiry, replay protection."""
import time
import unittest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tally_client import MutationGate, MutationTokenError


class TestMutationGate(unittest.TestCase):
    def test_issue_consume_ok(self):
        g = MutationGate(ttl_seconds=60)
        tok, exp = g.issue("Create:Ledger", "Acme", "hash1", "label")
        self.assertTrue(tok)
        self.assertGreater(exp, time.time())
        g.consume("Create:Ledger", "Acme", "hash1", tok)  # no raise

    def test_missing_token(self):
        g = MutationGate()
        with self.assertRaises(MutationTokenError):
            g.consume("Create:Ledger", "Acme", "hash1", "")

    def test_unknown_token(self):
        g = MutationGate()
        with self.assertRaises(MutationTokenError):
            g.consume("Create:Ledger", "Acme", "hash1", "bogus-token")

    def test_reuse_blocked(self):
        g = MutationGate()
        tok, _ = g.issue("Create:Ledger", "Acme", "hash1")
        g.consume("Create:Ledger", "Acme", "hash1", tok)
        with self.assertRaises(MutationTokenError):
            g.consume("Create:Ledger", "Acme", "hash1", tok)

    def test_expired_token(self):
        g = MutationGate(ttl_seconds=-1)  # already expired
        tok, _ = g.issue("Create:Ledger", "Acme", "hash1")
        with self.assertRaises(MutationTokenError):
            g.consume("Create:Ledger", "Acme", "hash1", tok)

    def test_wrong_operation(self):
        g = MutationGate()
        tok, _ = g.issue("Create:Ledger", "Acme", "hash1")
        with self.assertRaises(MutationTokenError):
            g.consume("Delete:Ledger", "Acme", "hash1", tok)

    def test_wrong_company(self):
        g = MutationGate()
        tok, _ = g.issue("Create:Ledger", "Acme", "hash1")
        with self.assertRaises(MutationTokenError):
            g.consume("Create:Ledger", "Other Co", "hash1", tok)

    def test_tampered_payload(self):
        g = MutationGate()
        tok, _ = g.issue("Create:Ledger", "Acme", "hash1")
        with self.assertRaises(MutationTokenError):
            g.consume("Create:Ledger", "Acme", "different-hash", tok)

    def test_extra_binding_mismatch(self):
        g = MutationGate()
        tok, _ = g.issue("Delete:Ledger", "Acme", "hash1",
                         extra={"target": "Cash"})
        with self.assertRaises(MutationTokenError):
            g.consume("Delete:Ledger", "Acme", "hash1", tok,
                      extra={"target": "Bank"})
        # matching extra works
        tok2, _ = g.issue("Delete:Ledger", "Acme", "hash1",
                          extra={"target": "Cash"})
        g.consume("Delete:Ledger", "Acme", "hash1", tok2,
                  extra={"target": "Cash"})

    def test_concurrent_reuse_single_winner(self):
        import threading
        g = MutationGate()
        tok, _ = g.issue("Create:Ledger", "Acme", "hash1")
        wins = []
        def attempt():
            try:
                g.consume("Create:Ledger", "Acme", "hash1", tok)
                wins.append(1)
            except MutationTokenError:
                pass
        threads = [threading.Thread(target=attempt) for _ in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(wins), 1)

    def test_tokens_unpredictable_and_unique(self):
        g = MutationGate()
        toks = {g.issue("Create:Ledger", "Acme", "h")[0] for _ in range(50)}
        self.assertEqual(len(toks), 50)
        for t in toks:
            self.assertGreaterEqual(len(t), 32)

    def test_storage_bounded(self):
        g = MutationGate(ttl_seconds=3600, max_tokens=10)
        for i in range(25):
            g.issue("Create:Ledger", "Acme", f"hash{i}")
        self.assertLessEqual(len(g._tokens), 10)


if __name__ == "__main__":
    unittest.main()
