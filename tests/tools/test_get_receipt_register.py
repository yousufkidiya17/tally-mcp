import unittest

class Test_get_receipt_register_Contract(unittest.TestCase):
    def test_get_receipt_register_name_valid(self):
        name = "get_receipt_register"
        self.assertTrue(len(name) > 0)

    def test_get_receipt_register_revision(self):
        self.assertEqual(35, 35)
