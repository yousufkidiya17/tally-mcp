"""Smoke tests: package imports, FastMCP registration (69 tools),
and the configured entry point -- all offline, no Tally needed."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestRegistration(unittest.TestCase):
    def test_server_imports(self):
        import server  # noqa
        import tally_client  # noqa
        import tally_pdf  # noqa

    def test_70_tools_registered(self):
        import server
        async def _list():
            return await server.mcp.list_tools()
        tools = asyncio.run(_list())
        self.assertEqual(len(tools), 70)
        names = [t.name for t in tools]
        self.assertEqual(len(set(names)), 70, "duplicate tool names!")

    def test_no_tool_removed_or_renamed(self):
        import server
        async def _list():
            return await server.mcp.list_tools()
        tools = asyncio.run(_list())
        names = {t.name for t in tools}
        # spot-check the full surface across categories
        expected = {
            "tally_status", "sync_masters",
            "create_primary_group", "create_group", "alter_group",
            "delete_group", "list_groups",
            "create_party_ledger", "create_bank_ledger", "create_cash_ledger",
            "create_sales_purchase_ledger", "create_expense_income_ledger",
            "create_tax_ledger", "create_standard_ledger",
            "delete_ledger", "alter_ledger", "alter_party_ledger", "list_ledgers",
            "create_vouchertype", "delete_vouchertype", "alter_vouchertype",
            "list_vouchertypes",
            "create_stockgroup", "delete_stockgroup", "alter_stockgroup",
            "create_stockcategory", "delete_stockcategory",
            "alter_stockcategory",
            "create_stockitem", "alter_stockitem", "delete_stockitem",
            "list_stockitems",
            "create_godown", "delete_godown", "alter_godown", "list_godowns",
            "create_unit", "alter_unit", "delete_unit", "list_units",
            "create_purchase_voucher", "create_sales_voucher",
            "create_payment_voucher", "create_receipt_voucher",
            "create_contra_voucher", "create_journal_voucher",
            "create_debit_note", "create_credit_note", "delete_voucher",
            "get_stock_summary", "get_party_outstanding",
            "check_ledger_exists", "check_stockitem_exists",
            "get_day_book", "get_ledger_report", "get_trial_balance",
            "get_profit_loss", "get_balance_sheet", "get_ageing_report",
            "get_godown_summary", "get_gstr1", "get_gstr3b",
            "get_sales_register", "get_purchase_register", "get_cash_flow",
            "get_payment_register", "get_receipt_register",
            "get_bank_reconciliation", "get_gstr9", "export_invoice_pdf",
        }
        self.assertEqual(names, expected)

    def test_mutation_tools_require_token_param(self):
        import server
        async def _list():
            return await server.mcp.list_tools()
        tools = {t.name: t for t in asyncio.run(_list())}
        sample = ["create_party_ledger", "alter_group", "delete_voucher",
                  "create_journal_voucher", "delete_ledger"]
        for name in sample:
            props = tools[name].inputSchema.get("properties", {})
            self.assertIn("confirm_token", props, name)
            self.assertTrue(props["preview"].get("default"), name)

    def test_read_tools_keep_preview_false_default(self):
        import server
        async def _list():
            return await server.mcp.list_tools()
        tools = {t.name: t for t in asyncio.run(_list())}
        for name in ["list_ledgers", "list_stockitems", "tally_status"]:
            props = tools[name].inputSchema.get("properties", {})
            if "preview" in props:
                self.assertFalse(props["preview"].get("default"), name)

    def test_entry_point_module(self):
        # server.py's __main__ block calls mcp.run(); importing must not
        # start the server (no side effects on import).
        import server
        self.assertTrue(hasattr(server, "mcp"))


if __name__ == "__main__":
    unittest.main()
