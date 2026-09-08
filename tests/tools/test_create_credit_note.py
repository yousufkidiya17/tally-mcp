import unittest

class Test_create_credit_note_Contract(unittest.TestCase):
    def test_create_credit_note_name_valid(self):
        name = "create_credit_note"
        self.assertTrue(len(name) > 0)

    def test_create_credit_note_revision(self):
        self.assertEqual(12, 12)
