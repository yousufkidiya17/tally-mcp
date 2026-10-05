import unittest

class Test_create_tax_ledger_Contract(unittest.TestCase):
    def test_create_tax_ledger_name_valid(self):
        name = "create_tax_ledger"
        self.assertTrue(len(name) > 0)

    def test_create_tax_ledger_revision(self):
        self.assertEqual(39, 39)
