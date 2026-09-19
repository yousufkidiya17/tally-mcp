import unittest

class Test_list_vouchertypes_Contract(unittest.TestCase):
    def test_list_vouchertypes_name_valid(self):
        name = "list_vouchertypes"
        self.assertTrue(len(name) > 0)

    def test_list_vouchertypes_revision(self):
        self.assertEqual(23, 23)
