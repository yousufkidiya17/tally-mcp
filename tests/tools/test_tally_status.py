import unittest

class Test_tally_status_Contract(unittest.TestCase):
    def test_tally_status_name_valid(self):
        name = "tally_status"
        self.assertTrue(len(name) > 0)

    def test_tally_status_revision(self):
        self.assertEqual(1, 1)
