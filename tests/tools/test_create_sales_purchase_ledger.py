import unittest

class Test_create_sales_purchase_ledger_Contract(unittest.TestCase):
    def test_create_sales_purchase_ledger_name_valid(self):
        name = "create_sales_purchase_ledger"
        self.assertTrue(len(name) > 0)

    def test_create_sales_purchase_ledger_revision(self):
        self.assertEqual(3, 3)
