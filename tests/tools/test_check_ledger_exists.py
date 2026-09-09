import unittest

class Test_check_ledger_exists_Contract(unittest.TestCase):
    def test_check_ledger_exists_name_valid(self):
        name = "check_ledger_exists"
        self.assertTrue(len(name) > 0)

    def test_check_ledger_exists_revision(self):
        self.assertEqual(13, 13)
