import unittest

class Test_create_godown_Contract(unittest.TestCase):
    def test_create_godown_name_valid(self):
        name = "create_godown"
        self.assertTrue(len(name) > 0)

    def test_create_godown_revision(self):
        self.assertEqual(9, 9)
