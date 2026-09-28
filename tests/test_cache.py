"""Tests for the sync cache: atomic writes, company scoping,
corrupt-file handling, and post-mutation invalidation."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tally_client as tc


class TestSyncCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_dir = tc._SYNC_DIR
        self.orig_file = tc._SYNC_FILE
        self.orig_lock = tc._SYNC_LOCK_FILE
        tc._SYNC_DIR = self.tmp.name
        tc._SYNC_FILE = os.path.join(self.tmp.name, "masters.json")
        tc._SYNC_LOCK_FILE = os.path.join(self.tmp.name, "masters.lock")
        os.environ["TALLY_COMPANY"] = "Company A"

    def tearDown(self):
        tc._SYNC_DIR = self.orig_dir
        tc._SYNC_FILE = self.orig_file
        tc._SYNC_LOCK_FILE = self.orig_lock
        os.environ.pop("TALLY_COMPANY", None)
        self.tmp.cleanup()

    def test_save_and_load_roundtrip(self):
        tc._sync_save([{"name": "Cash", "parent": "Cash-in-Hand"}], [])
        data, age_h = tc._sync_load()
        self.assertIsNotNone(data)
        self.assertEqual(data["ledgers"][0]["name"], "Cash")
        self.assertLess(age_h, 1)

    def test_company_scoping_no_cross_contamination(self):
        tc._sync_save([{"name": "CashA", "parent": "X"}], [])
        os.environ["TALLY_COMPANY"] = "Company B"
        data, _ = tc._sync_load()
        self.assertIsNone(data)  # B has no cache yet
        tc._sync_save([{"name": "CashB", "parent": "Y"}], [])
        data_b, _ = tc._sync_load()
        self.assertEqual(data_b["ledgers"][0]["name"], "CashB")
        os.environ["TALLY_COMPANY"] = "Company A"
        data_a, _ = tc._sync_load()
        self.assertEqual(data_a["ledgers"][0]["name"], "CashA")

    def test_corrupt_file_is_cache_miss(self):
        with open(tc._SYNC_FILE, "w") as f:
            f.write("{not valid json")
        data, age = tc._sync_load()
        self.assertIsNone(data)
        self.assertIsNone(age)

    def test_legacy_format_is_cache_miss(self):
        with open(tc._SYNC_FILE, "w") as f:
            json.dump({"ledgers": [], "_synced_at": 1}, f)
        data, _ = tc._sync_load()
        self.assertIsNone(data)

    def test_invalidate_company(self):
        tc._sync_save([{"name": "Cash", "parent": "X"}], [])
        tc._sync_invalidate_company("Company A")
        data, _ = tc._sync_load()
        self.assertIsNone(data)

    def test_no_tmp_files_left(self):
        tc._sync_save([{"name": "Cash", "parent": "X"}], [])
        leftovers = [f for f in os.listdir(self.tmp.name)
                     if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_missing_file_is_cache_miss(self):
        data, age = tc._sync_load()
        self.assertIsNone(data)
        self.assertIsNone(age)


if __name__ == "__main__":
    unittest.main()
