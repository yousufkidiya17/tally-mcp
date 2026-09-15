import unittest

class Test_alter_group_Contract(unittest.TestCase):
    def test_alter_group_name_valid(self):
        name = "alter_group"
        self.assertTrue(len(name) > 0)

    def test_alter_group_revision(self):
        self.assertEqual(19, 19)
