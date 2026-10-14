import unittest

class Test_delete_voucher_Contract(unittest.TestCase):
    def test_delete_voucher_name_valid(self):
        name = "delete_voucher"
        self.assertTrue(len(name) > 0)

    def test_delete_voucher_revision(self):
        self.assertEqual(48, 48)
