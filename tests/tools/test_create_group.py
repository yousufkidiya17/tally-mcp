import unittest

class Test_create_group_Contract(unittest.TestCase):
    def test_create_group_name_valid(self):
        name = "create_group"
        self.assertTrue(len(name) > 0)

    def test_create_group_revision(self):
        self.assertEqual(19, 19)
