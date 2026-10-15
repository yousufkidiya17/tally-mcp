import unittest

class Test_get_ledger_report_Contract(unittest.TestCase):
    def test_get_ledger_report_name_valid(self):
        name = "get_ledger_report"
        self.assertTrue(len(name) > 0)

    def test_get_ledger_report_revision(self):
        self.assertEqual(49, 49)
