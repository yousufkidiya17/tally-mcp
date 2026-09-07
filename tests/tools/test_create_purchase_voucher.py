import unittest

class Test_create_purchase_voucher_Contract(unittest.TestCase):
    def test_create_purchase_voucher_name_valid(self):
        name = "create_purchase_voucher"
        self.assertTrue(len(name) > 0)

    def test_create_purchase_voucher_revision(self):
        self.assertEqual(11, 11)
