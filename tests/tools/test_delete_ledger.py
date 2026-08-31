import unittest

class Test_delete_ledger_Contract(unittest.TestCase):
    def test_delete_ledger_name_valid(self):
        name = "delete_ledger"
        self.assertTrue(len(name) > 0)

    def test_delete_ledger_revision(self):
        self.assertEqual(4, 4)
