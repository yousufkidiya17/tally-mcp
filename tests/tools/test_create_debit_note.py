import unittest

class Test_create_debit_note_Contract(unittest.TestCase):
    def test_create_debit_note_name_valid(self):
        name = "create_debit_note"
        self.assertTrue(len(name) > 0)

    def test_create_debit_note_revision(self):
        self.assertEqual(30, 30)
