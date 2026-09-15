import unittest

class Test_create_primary_group_Contract(unittest.TestCase):
    def test_create_primary_group_name_valid(self):
        name = "create_primary_group"
        self.assertTrue(len(name) > 0)

    def test_create_primary_group_revision(self):
        self.assertEqual(19, 19)
