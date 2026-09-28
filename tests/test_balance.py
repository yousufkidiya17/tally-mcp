"""Tests for Decimal balance validation and the voucher builders."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tally_client as tc
from tally_client import ImbalanceError


class TestBalanceValidation(unittest.TestCase):
    def test_journal_balanced_ok(self):
        xml = tc.build_journal_voucher_message(
            date="20260401",
            debit_entries=[{"ledger": "Rent A/c", "amount": 5000}],
            credit_entries=[{"ledger": "Cash", "amount": 5000}],
        )
        self.assertIn("<VOUCHER", xml)

    def test_journal_imbalanced_refused(self):
        with self.assertRaises(ImbalanceError):
            tc.build_journal_voucher_message(
                date="20260401",
                debit_entries=[{"ledger": "Rent A/c", "amount": 5000}],
                credit_entries=[{"ledger": "Cash", "amount": 4999}],
            )

    def test_journal_boundary_inclusive(self):
        # exactly 0.01 imbalance passes (boundary inclusive)
        xml = tc.build_journal_voucher_message(
            date="20260401",
            debit_entries=[{"ledger": "A", "amount": 100.00}],
            credit_entries=[{"ledger": "B", "amount": 100.01}],
        )
        self.assertIn("<VOUCHER", xml)
        # 0.02 fails
        with self.assertRaises(ImbalanceError):
            tc.build_journal_voucher_message(
                date="20260401",
                debit_entries=[{"ledger": "A", "amount": 100.00}],
                credit_entries=[{"ledger": "B", "amount": 100.02}],
            )

    def test_journal_compound_balanced(self):
        xml = tc.build_journal_voucher_message(
            date="20260401",
            debit_entries=[{"ledger": "A", "amount": 300},
                           {"ledger": "B", "amount": 200.50}],
            credit_entries=[{"ledger": "C", "amount": 500.50}],
        )
        self.assertIn("<VOUCHER", xml)

    def test_nan_inf_rejected(self):
        for bad in [float("nan"), float("inf"), float("-inf")]:
            with self.assertRaises(ValueError, msg=f"{bad}"):
                tc.build_journal_voucher_message(
                    date="20260401",
                    debit_entries=[{"ledger": "A", "amount": bad}],
                    credit_entries=[{"ledger": "B", "amount": 1}],
                )

    def test_malformed_amount_rejected(self):
        for bad in ["abc", "", None, True]:
            with self.assertRaises(ValueError, msg=f"{bad!r}"):
                tc.build_payment_voucher_message(
                    date="20260401",
                    debit_entries=[{"ledger": "A", "amount": bad}],
                )

    def test_payment_auto_balances(self):
        xml = tc.build_payment_voucher_message(
            date="20260401",
            debit_entries=[{"ledger": "Supplier", "amount": 1500.75,
                            "bill_ref": "B1"}],
            credit_ledger="Cash",
        )
        self.assertIn("<VOUCHER", xml)

    def test_receipt_auto_balances(self):
        xml = tc.build_receipt_voucher_message(
            date="20260401",
            credit_entries=[{"ledger": "Customer", "amount": 2000}],
            debit_ledger="Cash",
        )
        self.assertIn("<VOUCHER", xml)

    def test_contra_validates_amount(self):
        xml = tc.build_contra_voucher_message(
            date="20260401", debit_ledger="Bank", credit_ledger="Cash",
            amount=10000)
        self.assertIn("<VOUCHER", xml)
        with self.assertRaises(ValueError):
            tc.build_contra_voucher_message(
                date="20260401", debit_ledger="Bank", credit_ledger="Cash",
                amount="not-a-number")

    def test_decimal_precision(self):
        # 0.1 + 0.2 style float dust must not trip the check
        xml = tc.build_journal_voucher_message(
            date="20260401",
            debit_entries=[{"ledger": "A", "amount": 0.1},
                           {"ledger": "B", "amount": 0.2}],
            credit_entries=[{"ledger": "C", "amount": 0.3}],
        )
        self.assertIn("<VOUCHER", xml)

    def test_client_converts_builder_error_to_refused(self):
        os.environ["TALLY_COMPANY"] = "Test Company"
        try:
            c = tc.TallyClient(base_url="http://127.0.0.1:9999/",
                               gate=tc.MutationGate())
            res = c.create_journal_voucher(
                date="20260401",
                debit_entries=[{"ledger": "A", "amount": 100}],
                credit_entries=[{"ledger": "B", "amount": 50}],
                preview=True)
            self.assertFalse(res.success)
            self.assertIn("REFUSED", res.message)
        finally:
            os.environ.pop("TALLY_COMPANY", None)


if __name__ == "__main__":
    unittest.main()
