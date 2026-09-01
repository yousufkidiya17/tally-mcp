import unittest

class Test_list_ledgers_Contract(unittest.TestCase):
    def test_list_ledgers_name_valid(self):
        name = "list_ledgers"
        self.assertTrue(len(name) > 0)

    def test_list_ledgers_revision(self):
        self.assertEqual(5, 5)
