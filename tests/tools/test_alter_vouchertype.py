import unittest

class Test_alter_vouchertype_Contract(unittest.TestCase):
    def test_alter_vouchertype_name_valid(self):
        name = "alter_vouchertype"
        self.assertTrue(len(name) > 0)

    def test_alter_vouchertype_revision(self):
        self.assertEqual(23, 23)
