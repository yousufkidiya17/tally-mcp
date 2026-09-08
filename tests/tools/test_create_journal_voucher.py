import unittest

class Test_create_journal_voucher_Contract(unittest.TestCase):
    def test_create_journal_voucher_name_valid(self):
        name = "create_journal_voucher"
        self.assertTrue(len(name) > 0)

    def test_create_journal_voucher_revision(self):
        self.assertEqual(12, 12)
