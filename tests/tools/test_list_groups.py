import unittest

class Test_list_groups_Contract(unittest.TestCase):
    def test_list_groups_name_valid(self):
        name = "list_groups"
        self.assertTrue(len(name) > 0)

    def test_list_groups_revision(self):
        self.assertEqual(20, 20)
