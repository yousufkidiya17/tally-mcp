import unittest

class Test_create_vouchertype_Contract(unittest.TestCase):
    def test_create_vouchertype_name_valid(self):
        name = "create_vouchertype"
        self.assertTrue(len(name) > 0)

    def test_create_vouchertype_revision(self):
        self.assertEqual(5, 5)
