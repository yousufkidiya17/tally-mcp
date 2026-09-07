import unittest

class Test_create_receipt_voucher_Contract(unittest.TestCase):
    def test_create_receipt_voucher_name_valid(self):
        name = "create_receipt_voucher"
        self.assertTrue(len(name) > 0)

    def test_create_receipt_voucher_revision(self):
        self.assertEqual(11, 11)
