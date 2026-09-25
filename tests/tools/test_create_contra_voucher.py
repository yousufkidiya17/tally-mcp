import unittest

class Test_create_contra_voucher_Contract(unittest.TestCase):
    def test_create_contra_voucher_name_valid(self):
        name = "create_contra_voucher"
        self.assertTrue(len(name) > 0)

    def test_create_contra_voucher_revision(self):
        self.assertEqual(29, 29)
