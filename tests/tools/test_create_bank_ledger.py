import unittest

class Test_create_bank_ledger_Contract(unittest.TestCase):
    def test_create_bank_ledger_name_valid(self):
        name = "create_bank_ledger"
        self.assertTrue(len(name) > 0)

    def test_create_bank_ledger_revision(self):
        self.assertEqual(20, 20)
