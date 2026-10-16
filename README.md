# Tally Agent 🤖

![Python](https://img.shields.io/badge/python-3.12-blue)
![MCP](https://img.shields.io/badge/MCP-compatible-brightgreen)
![Tools](https://img.shields.io/badge/tools-70-orange)
![Tally Prime](https://img.shields.io/badge/Tally-Prime-red)
![License](https://img.shields.io/badge/license-MIT-yellow)

**Professional MCP Server for Tally Prime — let any AI agent do accounting.**

Complete Tally Prime integration with **70 verified tools** — Masters, Transactions, Inventory, GST Reports, all working via Tally's XML API. No Tally UI needed.

---

## What is Tally Agent?

Tally Agent is an MCP (Model Context Protocol) server that lets any AI agent work with **Tally Prime** — India's most popular accounting software. Create ledgers, record vouchers, generate reports, all through simple tool calls.

**No Tally UI needed.** Everything works via Tally's HTTP XML API (port 9999).

---

## Quick Start

### 1. Connect to Tally

```
Tally Prime must be running with the company open.
Default: http://localhost:9999/
```

### 2. Check Connection

```
tally_status() → {"connected": true, "company": "Your Company Name"}
```

### 3. Sync Masters

```
sync_masters() → Caches all ledgers and stock items for fast lookup
```

### 4. Mandatory Workflow

```
1. tally_status()           → Verify Tally is reachable
2. sync_masters()            → Load master data
3. check_ledger_exists()     → Verify party exists before voucher
4. preview=True (default)    → Preview XML + get confirm_token (nothing sent)
5. User approval              → Get explicit confirmation
6. preview=False + token      → Execute (server enforces the token)
7. Verify                    → Read back and confirm
```

**⚠️ NEVER write without preview + user approval.**
**⚠️ NEVER delete without explicit approval.**
**⚠️ NEVER create unrequested masters.**

---

## 🔒 Security Model

**Two-step writes (enforced in code, not just convention).** Every
create/alter/delete tool defaults to `preview=True`: it builds the exact XML,
returns it plus a short-lived, single-use `confirm_token`, and sends nothing.
Executing requires calling again with `preview=False` and that token. The
server verifies the token is valid, unexpired, unused, bound to the same
operation/company/payload, then dispatches exactly once. Missing, expired,
reused, or tampered tokens are refused with zero HTTP requests to Tally.
A token proves the exact preview ran — it does not prove a human reviewed it.

**Company pinning.** All mutations are pinned to the `TALLY_COMPANY`
environment variable (fail-closed when unset) and stamped into every request
as `<SVCURRENTCOMPANY>`. There is no silent fallback to whichever company
happens to be open.

**Delete validation.** Before previewing a delete, the exact target is
re-queried from the pinned company (fresh export, never the cache); missing
or ambiguous targets fail closed. Groups must be empty. The verified target
is bound into the token and re-checked immediately before execution.

**Transport.** Tally's XML API is plain HTTP with no encryption or
authentication. Run Tally on `localhost` or a trusted private network only.
For remote access, put an authenticated TLS-terminating proxy in front of
port 9999 — the confirm_token gate does NOT protect an exposed Tally
endpoint from other clients.

**Local cache.** `sync/masters.json` caches ledgers/stock items per company
for up to 4 hours (atomic writes, locked). Destructive checks always use
fresh exports, and any successful mutation invalidates the company's cache.

---

## Tool Categories (69 Tools)

### 🔄 Sync (4)
| Tool | Purpose |
|------|---------|
| `tally_status` | Check Tally connection |
| `sync_masters` | Cache all ledgers + stock items |
| `check_ledger_exists` | Verify ledger exists |
| `check_stockitem_exists` | Verify stock item exists |

### 👥 Groups (5)
| Tool | Purpose |
|------|---------|
| `create_primary_group` | Top-level group (e.g. "My Assets") |
| `create_group` | Sub-group under parent |
| `alter_group` | Rename/modify group |
| `delete_group` | Delete group |
| `list_groups` | List all groups |

### 📒 Ledgers (10)
| Tool | Purpose |
|------|---------|
| `create_party_ledger` | Customer/supplier (Sundry Debtors/Creditors) |
| `create_cash_ledger` | Cash account |
| `create_bank_ledger` | Bank account with IFSC, A/c no |
| `create_sales_purchase_ledger` | Sales/Purchase account |
| `create_expense_income_ledger` | Expense/Income (direct/indirect) |
| `create_tax_ledger` | Tax ledger (Duties & Taxes) |
| `create_standard_ledger` | Any standard group ledger |
| `alter_ledger` | Modify ledger |
| `delete_ledger` | Delete ledger |
| `list_ledgers` | List all ledgers |

### 🧾 Voucher Types (4)
| Tool | Purpose |
|------|---------|
| `create_vouchertype` | Custom voucher type |
| `alter_vouchertype` | Rename voucher type |
| `delete_vouchertype` | Delete voucher type |
| `list_vouchertypes` | List all voucher types |

### 📦 Inventory (18)
| Tool | Purpose |
|------|---------|
| `create_stockgroup` / `alter_stockgroup` / `delete_stockgroup` | Stock groups |
| `create_stockcategory` / `alter_stockcategory` / `delete_stockcategory` | Stock categories |
| `create_stockitem` / `alter_stockitem` / `delete_stockitem` | Stock items |
| `list_stockitems` | List all items |
| `create_godown` / `alter_godown` / `delete_godown` | Warehouses |
| `list_godowns` | List all godowns |
| `create_unit` / `alter_unit` / `delete_unit` | Units (kg, nos) |
| `list_units` | List all units |

### 💰 Transactions (9)
| Tool | Purpose |
|------|---------|
| `create_purchase_voucher` | Purchase bill (with GST) |
| `create_sales_voucher` | Sales bill (cash/credit, with GST) |
| `create_payment_voucher` | Payment to supplier |
| `create_receipt_voucher` | Receipt from customer |
| `create_contra_voucher` | Cash↔Bank transfer |
| `create_journal_voucher` | Journal entry |
| `create_debit_note` | Purchase return |
| `create_credit_note` | Sales return |
| `delete_voucher` | Delete any voucher |

---

## 📚 Documentation

| File | Purpose |
|------|---------|
| `README.md` | Full guide (this file) |
| `QUICKSTART.md` | New agent quick start |
| `TECHNICAL.md` | XML API technical details |

---

### 📊 Reports (19)
| Tool | Purpose |
|------|---------|
| `get_day_book` | Daily vouchers (date/type filter) |
| `get_ledger_report` | One party's full history + balance |
| `get_trial_balance` | All ledgers with balances |
| `get_profit_loss` | P&L statement |
| `get_balance_sheet` | Balance Sheet |
| `get_cash_flow` | Cash movement |
| `get_stock_summary` | Stock on hand per item |
| `get_godown_summary` | Stock by godown/location |
| `get_party_outstanding` | Who owes whom |
| `get_ageing_report` | Outstanding by age (0-30/60/90+ days) |
| `get_sales_register` | All sales with totals |
| `get_purchase_register` | All purchases with totals |
| `get_payment_register` | All payments with totals |
| `get_receipt_register` | All receipts with totals |
| `get_bank_reconciliation` | Reconciled vs unreconciled |
| `get_gstr1` | GSTR-1 (B2B/B2CL/B2CS/CDNR) |
| `get_gstr3b` | GSTR-3B (Table 3.1 + 4) |
| `get_gstr9` | GSTR-9 annual summary |
| `export_invoice_pdf` | Sales invoice as PDF |

---

## Common Workflows

### Record a Purchase
```python
# 1. Check stock item exists
check_stockitem_exists("RICE BASMATI Noor 30kg")

# 2. Check party exists  
check_ledger_exists("ABC Suppliers")

# 3. Preview
create_purchase_voucher(
    date="20240401",
    party_name="ABC Suppliers",
    items=[{"item": "Widget A", "qty": 10, "rate": 500}],
    preview=True
)

# 4. User approves → execute with preview=False
```

### Record a Sale
```python
create_sales_voucher(
    date="20240401",
    party_name="Cash",  # or customer name for credit
    buyer_name="Walk-in Customer",  # for cash sale bill print
    items=[{"item": "RICE BASMATI Noor 30kg", "qty": 1, "rate": 9000}],
    preview=True
)
```

### Check Outstanding
```python
get_party_outstanding()  # All parties
get_party_outstanding(party_name="ABC Suppliers")  # Specific party
```

### Generate GSTR-1
```python
get_gstr1(date_from="20240401", date_to="20240430")
# Returns B2B, B2CL, B2CS, CDNR tables
```

---

## Language Rule

**Match the user's language:**
- User speaks Hinglish → Reply in Hinglish
- User speaks English (professional) → Reply in English
- Always use SIMPLE words, no jargon, short sentences

---

## Technical Details

- **Protocol:** MCP (Model Context Protocol)
- **Transport:** stdio
- **Tally API:** HTTP XML on port 9999
- **Company:** Set via `TALLY_COMPANY` env var
- **Prefix:** Set via `TALLY_PREFIX` env var (default: TXN)

### Files
```
tally-mcp/
├── server.py          # MCP server (69 tools)
├── tally_client.py    # Tally XML client
├── requirements.txt   # Dependencies
├── README.md          # This file
├── QUICKSTART.md      # Agent quick start guide
└── sync/
    └── masters.json   # Cached masters (auto-refresh 4h)
```

---

## Verified

Every tool tested against live Tally Prime. XML shapes verified 2026-09-28 through 2026-10-05.

**Built with ❤️ for Tally users**
