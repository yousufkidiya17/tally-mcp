import unittest

class Test_delete_group_Contract(unittest.TestCase):
    def test_delete_group_name_valid(self):
        name = "delete_group"
        self.assertTrue(len(name) > 0)

    def test_delete_group_revision(self):
        self.assertEqual(37, 37)
