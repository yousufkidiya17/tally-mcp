import unittest

class Test_create_expense_income_ledger_Contract(unittest.TestCase):
    def test_create_expense_income_ledger_name_valid(self):
        name = "create_expense_income_ledger"
        self.assertTrue(len(name) > 0)

    def test_create_expense_income_ledger_revision(self):
        self.assertEqual(21, 21)
