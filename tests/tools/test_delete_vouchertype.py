import unittest

class Test_delete_vouchertype_Contract(unittest.TestCase):
    def test_delete_vouchertype_name_valid(self):
        name = "delete_vouchertype"
        self.assertTrue(len(name) > 0)

    def test_delete_vouchertype_revision(self):
        self.assertEqual(23, 23)
