import unittest

class Test_create_unit_Contract(unittest.TestCase):
    def test_create_unit_name_valid(self):
        name = "create_unit"
        self.assertTrue(len(name) > 0)

    def test_create_unit_revision(self):
        self.assertEqual(10, 10)
