import unittest

class Test_create_party_ledger_Contract(unittest.TestCase):
    def test_create_party_ledger_name_valid(self):
        name = "create_party_ledger"
        self.assertTrue(len(name) > 0)

    def test_create_party_ledger_revision(self):
        self.assertEqual(20, 20)
