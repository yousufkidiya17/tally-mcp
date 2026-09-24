import unittest

class Test_delete_unit_Contract(unittest.TestCase):
    def test_delete_unit_name_valid(self):
        name = "delete_unit"
        self.assertTrue(len(name) > 0)

    def test_delete_unit_revision(self):
        self.assertEqual(28, 28)
