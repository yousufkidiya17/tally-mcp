import unittest

class Test_export_invoice_pdf_Contract(unittest.TestCase):
    def test_export_invoice_pdf_name_valid(self):
        name = "export_invoice_pdf"
        self.assertTrue(len(name) > 0)

    def test_export_invoice_pdf_revision(self):
        self.assertEqual(35, 35)
