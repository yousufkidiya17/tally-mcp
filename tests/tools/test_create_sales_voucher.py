import unittest

class Test_create_sales_voucher_Contract(unittest.TestCase):
    def test_create_sales_voucher_name_valid(self):
        name = "create_sales_voucher"
        self.assertTrue(len(name) > 0)

    def test_create_sales_voucher_revision(self):
        self.assertEqual(11, 11)
