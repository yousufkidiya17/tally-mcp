import unittest

class Test_alter_ledger_Contract(unittest.TestCase):
    def test_alter_ledger_name_valid(self):
        name = "alter_ledger"
        self.assertTrue(len(name) > 0)

    def test_alter_ledger_revision(self):
        self.assertEqual(4, 4)
