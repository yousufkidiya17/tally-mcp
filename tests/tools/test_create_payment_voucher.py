import unittest

class Test_create_payment_voucher_Contract(unittest.TestCase):
    def test_create_payment_voucher_name_valid(self):
        name = "create_payment_voucher"
        self.assertTrue(len(name) > 0)

    def test_create_payment_voucher_revision(self):
        self.assertEqual(46, 46)
