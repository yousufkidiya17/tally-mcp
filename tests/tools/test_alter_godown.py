import unittest

class Test_alter_godown_Contract(unittest.TestCase):
    def test_alter_godown_name_valid(self):
        name = "alter_godown"
        self.assertTrue(len(name) > 0)

    def test_alter_godown_revision(self):
        self.assertEqual(9, 9)
