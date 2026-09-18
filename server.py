#!/usr/bin/env python3
"""Tally Prime MCP server (XML API via port 9999).

Exposes verified Tally Prime master-management operations as MCP tools.

Write safety (enforced in code): every create/alter/delete tool defaults to
`preview=True`, which returns the exact XML that *would* be sent without
touching Tally, plus a single-use `confirm_token`. To execute, call the same
tool with `preview=False` and that token. Execution without a valid token is
refused and nothing is sent. This honours the standing rule "no entry without
asking". A token proves the exact preview ran; it does not prove human review.

All mutations are pinned to the TALLY_COMPANY environment variable
(fail-closed when unset) and stamped into the request envelope as
<SVCURRENTCOMPANY>.

Run:
    .venv/bin/python server.py          # stdio transport (for MCP clients)

Configure:
    TALLY_URL env var (default http://localhost:9999/)
    TALLY_COMPANY env var (required for any create/alter/delete)
"""

import os
import re

from mcp.server.fastmcp import FastMCP

from tally_client import (
    TallyClient,
    CompanyNotConfiguredError,
    UnsafeXmlError,
    XmlParseError,
    _parse_xml_safe,
    get_pinned_company,
    xml_escape,
)

mcp = FastMCP(
    "tally-agent",
    instructions=(
        "Tally Agent -- Professional Tally Prime MCP Server (69 tools). "
        "Complete accounting: Masters, Transactions, Reports via Tally's XML API (port 9999). "
        "Every tool was live-tested against a real Tally company.\n\n"
        "MANDATORY WORKFLOW:\n"
        "1. Call tally_status FIRST. If Tally is unreachable, stop and tell the user.\n"
        "2. Call sync_masters at the START of each work session (it checks Tally "
        "connection itself, then caches all ledgers + stock items locally).\n"
        "3. Before creating any voucher, use check_ledger_exists / check_stockitem_exists "
        "to verify masters (they auto-refresh if cache is older than 4 hours).\n"
        "4. Write tools DEFAULT to preview=True and enforce a two-step flow: "
        "preview first (returns the exact XML plus a single-use confirm_token), "
        "show the XML to the user, and only then execute with preview=False and "
        "that confirm_token. The server rejects execution without a valid token; "
        "a token proves the exact preview ran, not that a human reviewed it.\n"
        "5. Deletes are validated before preview (exact target in the pinned "
        "company; groups must be empty) and re-checked before execution.\n"
        "6. Never create, alter or delete a master the user did not ask for. "
        "Never use CREATE to check existence (use the check_* tools).\n\n"
        "BILL ENTRY WORKFLOW GUIDE - READ THIS BEFORE EVERY BILL ENTRY:\n"
        "Step 1 - READ THE BILL CAREFULLY:\n"
        "- Check if the bill has GST. Look for 'Exempted Under GST', 'GSTIN', "
        "tax amounts, HSN codes. If the bill says Exempted/No GST, use "
        "non-GST groups and items. NEVER create GST-type items for a "
        "non-GST bill.\n"
        "- Note the supplier name, bill number, date, and all item details "
        "(qty, unit, rate, amount).\n\n"
        "Step 2 - SUPPLIER LEDGER:\n"
        "- Check if supplier exists with check_ledger_exists.\n"
        "- If not, ALWAYS use create_party_ledger with party_type='supplier'. "
        "Never use low-level builders directly.\n"
        "- Include all details: address, state, pincode, GSTIN (if on bill), "
        "registration_type.\n\n"
        "Step 3 - STOCK ITEMS (MOST IMPORTANT - ASK THE USER):\n"
        "- NEVER guess item names from handwritten bills. Handwriting is often "
        "unclear. ALWAYS ask the user: 'Bill me ye item kya likha hai?'\n"
        "- Before creating a NEW item, check existing items with "
        "list_stockitems. Many traders use the SAME Tally item for different "
        "bill names. Example: bill says 'PP Rice' but Tally has 'Parmal Rice' "
        "-- ask the user: 'Kya ye Parmal Rice me daalun ya naya item banau?'\n"
        "- Check the bill for GST. If exempt, put items in the non-GST group "
        "(e.g. Basmati Rice). If taxable, use the taxable group. NEVER assume "
        "-- ask if unsure.\n"
        "- NEVER assume HSN code or tax rate. Always ask the user.\n\n"
        "Step 4 - VOUCHER:\n"
        "- Only create the voucher AFTER supplier and items are confirmed.\n"
        "- Use preview=True first, show the user, get confirmation, then execute.\n"
        "- If Tally returns an error, do NOT retry blindly. Read the error, "
        "understand it, and tell the user what went wrong.\n\n"
        "GROUP SECTION (5 tools) - Masters Phase 1 (Accounting), Step 1: Groups.\n"
        "- create_primary_group: top-level group (Tally 'Under: Primary'). "
        "Needs nature: Assets / Liabilities / Income / Expenses. "
        "Use when the group stands alone.\n"
        "- create_group: sub-group under an EXISTING group (parent required). "
        "Nature is inherited from the parent - do not pass it.\n"
        "- alter_group: rename (new_name), move (parent='Primary' -> top-level, "
        "needs nature; parent='<group>' -> sub-group), re-classify nature "
        "(primary groups only), change flags. Only the fields you pass change.\n"
        "- delete_group: permanent. The group must be empty (no ledgers/sub-groups).\n"
        "- list_groups: read-only. parent='' -> all groups; 'Primary' -> top-level "
        "only; '<group>' -> its direct sub-groups.\n"
        "Known Tally XML quirk: 'Method to Allocate' (ADDLALLOCTYPE) is silently "
        "ignored via XML - it must be set in Tally's UI, not via these tools.\n\n"
        "LEDGER SECTION (10 tools) - Masters Phase 1 (Accounting), Step 2: Ledgers. Pick ONE tool by the "
        "target Under-group; never force a group through the wrong tool:\n"
        "- list_ledgers: read-only, all ledgers with their parent groups.\n"
        "- create_party_ledger: ONLY Sundry Debtors (customer) / Sundry Creditors "
        "(supplier). Bill-wise, credit limit/period, PAN, GSTIN, mailing. "
        "party_type='customer' -> Debtors, 'supplier' -> Creditors. "
        "ALWAYS use this for party ledgers, never the low-level builder.\n"
        "- alter_party_ledger: update an EXISTING party ledger's fields "
        "(address, GSTIN, bill-wise, etc.). CRITICAL: Tally XML ALTER "
        "overwrites -- always send ALL fields you want to keep, not just "
        "the changed ones, or they will be cleared.\n"
        "- create_bank_ledger: ONLY Bank Accounts / Bank OD A/c. Account number, "
        "IFSC, branch, holder, GSTIN in one call.\n"
        "- create_cash_ledger: Cash-in-Hand ledgers (extra cash books like Petty Cash).\n"
        "- create_sales_purchase_ledger: ONLY Sales Accounts / Purchase Accounts. "
        "kind='sales' or 'purchase'.\n"
        "- create_expense_income_ledger: ONLY Direct/Indirect Expenses and "
        "Direct/Indirect Incomes. ledger_group picks the group.\n"
        "- create_tax_ledger: Duties & Taxes for OTHER tax ledgers (e.g. Cess). "
        "REFUSES the names CGST/SGST/IGST - Tally auto-creates those, never "
        "recreate them manually.\n"
        "- create_standard_ledger: ANY of the 20 standard-form groups (Capital "
        "Account, Current Assets, Fixed Assets, Loans, Suspense A/c, etc.) by "
        "exact group name. Refuses non-standard groups - use the dedicated tool.\n"
        "- delete_ledger: permanent true delete. Some ledgers (e.g. Purchase A/c) "
        "may be refused by Tally with 'Cannot be deleted!' - report it, do not retry.\n"
        "Known Tally XML quirks: (1) the visible 'GST applicability' field is "
        "<GSTAPPLICABLE>&#4; Applicable/Not Applicable</GSTAPPLICABLE>, NOT "
        "<ISGSTAPPLICABLE>; (2) mailing-address ALTER silently fails unless "
        "APPLICABLEFROM is the company's books-begin date; (3) tax-ledger "
        "rounding method only accepts 'Not Applicable' via XML - real values "
        "need Tally's UI; (4) Tally CRASHES if the same master is created twice - "
        "always check existence with list_* first.\n\n"
        "VOUCHER TYPE SECTION (2 tools) - Masters Phase 1 (Accounting), Step 3: Voucher Types.\n"
        "- create_vouchertype: new voucher type under an EXISTING base type. "
        "parent MUST be one of Tally's base types (Payment, Receipt, Journal, "
        "Sales, Purchase, Contra, etc.) - 'Primary' does not work.\n"
        "- delete_vouchertype: permanent delete. Never use on Tally's 24 default "
        "voucher types unless the user explicitly asks.\n\n"
        "INVENTORY SECTION (14 tools) - Masters Phase 2 (Inventory): Stock & Godown. "
        "Order: Stock Group -> Stock Category -> Stock Item -> Godown.\n"
        "- create_stockgroup / delete_stockgroup: NO parent needed (Tally rejects "
        "'Primary'). HSN/GST go via ALTER with <SRCOFHSNDETAILS>Specify Details "
        "Here</SRCOFHSNDETAILS> + <SRCOFGSTDETAILS>Specify Details Here</SRCOFGSTDETAILS> "
        "- without these Tally keeps 'As per Company/Stock Group'.\n"
        "- create_stockcategory / delete_stockcategory: simple, no parent.\n"
        "- create_stockitem / alter_stockitem / delete_stockitem / list_stockitems: "
        "item's group via <PARENT> with the PLAIN group name (never the &#4; export "
        "prefix - Tally silently ignores it). Non-GST items need "
        "<GSTAPPLICABLE>&#4; Not Applicable</GSTAPPLICABLE>. GST items need "
        "HSNDETAILS.LIST + GSTDETAILS.LIST (CGST=rate/2, SGST=rate/2, IGST=rate). "
        "NEVER assume HSN code or tax rate - always ask the user.\n"
        "- create_godown / delete_godown: simple; optional <PARENT> for sub-godowns.\n"
        "- get_stock_summary: read-only stock on hand per item (ClosingBalance). "
        "Use before a sales entry to check availability.\n"
        "- get_day_book: daily vouchers (date/type filter). "
        "get_ledger_report: one ledger's full history with Dr/Cr/balance. "
        "get_trial_balance: all ledgers. get_profit_loss: P&L. "
        "get_balance_sheet: assets vs liabilities. "
        "get_ageing_report: receivable/payable by age buckets (0-30/30-60/60-90/90+). get_godown_summary: stock by godown.\n"
        "USER LANGUAGE: Match the user's language. If they speak Hinglish, reply in Hinglish. "
        "If they speak English (professional), reply in English. "
        "Always use SIMPLE words, no jargon, short sentences.\n"
        "Requires 'Maintain multiple godowns' ON in the company (F11).\n"
        "Units: create_unit / delete_unit - simple units (e.g. kg, Nos).\n"
        "Verified XML: <UNIT Action=\"Create\"><NAME>X</NAME>"
        "<ISSIMPLEUNIT>Yes</ISSIMPLEUNIT></UNIT> (NAME is a child tag; "
        "NAME attribute gives BAD UNIT NAME). Create the unit BEFORE any "
        "stock item references it.\n\n"
        "TRANSACTION SECTION (3 tools) - Transactions (masters ke baad ka kaam). Masters must exist "
        "before any voucher (party ledger, stock items, purchase/sales ledgers).\n"
        "- create_purchase_voucher: item-invoice purchase. Supplier block FIRST "
        "with ISPARTYLEDGER=Yes and positive amount; items in "
        "ALLINVENTORYENTRIES.LIST with negative amounts; extra charges "
        "(Insurance/Hammali/Freight) as voucher-level LEDGERENTRIES.LIST with "
        "only 3 tags (LEDGERNAME + ISDEEMEDPOSITIVE=Yes + negative AMOUNT) - "
        "NEVER put ISPARTYLEDGER on a charge, or Day Book breaks. Party Details "
        "do NOT auto-fill on XML import - this tool injects them explicitly. "
        "Bill date MUST be within the company's books period.\n"
        "Payment / Receipt voucher tools are not built yet.\n"
        "- create_sales_voucher: Sales item-invoice (credit/cash). EXACT MIRROR of purchase: "
        "customer block FIRST (ISDEEMEDPOSITIVE=Yes, ISPARTYLEDGER=Yes, NEGATIVE amount); "
        "items/charges/tax POSITIVE with ISDEEMEDPOSITIVE=No; credit sales get "
        "BILLALLOCATIONS.LIST, cash sales use the cash ledger with no bill allocations. "
        "sale_type='credit' or 'cash'. buyer_name: optional walk-in buyer name for cash "
        "sales (prints on invoice via PARTYNAME/BASICBUYERNAME, ledger stays Cash, "
        "no new ledger created).\n"
        "SALES WORKFLOW: 1) get_stock_summary se stock check karo (jitna hai utna becho), "
        "2) item ka GST status dekho - GST sirf GST-applicable items pe lagao "
        "(non-GST item pe tax mat lagao), 3) credit ya cash sale banao, "
        "4) Day Book me verify karo. Cash sales need NO separate Tally setting - "
        "same Sales voucher, bas Party me Cash ledger.\n"
        "- delete_voucher: permanent true delete by REMOTEID (UPPERCASE "
        "ACTION=\"Delete\" via Import Data, REPORTNAME=Vouchers). "
        "Tool-created vouchers have explicit REMOTEIDs; UI-created vouchers "
        "with Tally-internal IDs must be deleted in Tally's UI, not via XML."
    ),
)

TALLY_URL = os.environ.get("TALLY_URL", "http://localhost:9999/")


def _client() -> TallyClient:
    return TallyClient(base_url=TALLY_URL)


_INVOICE_DIR = os.path.join(os.path.expanduser("~"), "tally_invoices")


def _safe_invoice_filename(voucher_number: str) -> str:
    """Deterministic safe filename for a voucher number.

    The voucher number is treated as an identifier, never a path:
    separators, traversal segments, control characters and absolute/
    Windows-style paths are neutralized. The returned name is always of
    the form invoice_<sanitized>.pdf.
    """
    raw = (voucher_number or "").strip()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw)
    safe = re.sub(r"_+", "_", safe).strip("._")[:80].strip("._")
    if not safe or safe in (".", ".."):
        raise ValueError(
            f"Invalid voucher_number for a filename: {voucher_number!r}.")
    return f"invoice_{safe}.pdf"


def _resolve_invoice_path(voucher_number: str) -> str:
    """Resolve the invoice PDF path, contained in ~/tally_invoices.

    Verifies via realpath containment (not a string-prefix check) so
    traversal, absolute paths and symlink escapes cannot leave the export
    root. On filename collision a numeric suffix is added instead of
    silently replacing an unrelated file.
    """
    root = os.path.realpath(_INVOICE_DIR)
    os.makedirs(root, exist_ok=True)
    dest = os.path.realpath(os.path.join(root, _safe_invoice_filename(voucher_number)))
    if os.path.commonpath([root, dest]) != root:
        raise ValueError(
            "Resolved invoice path escapes the export directory -- refused.")
    if os.path.exists(dest):
        base, ext = os.path.splitext(dest)
        i = 2
        while os.path.exists(f"{base}-{i}{ext}"):
            i += 1
        dest = f"{base}-{i}{ext}"
    return dest


# ---------------------------------------------------------------------------
# 1. connection check -- ALWAYS call this first before any other tool
# ---------------------------------------------------------------------------

@mcp.tool()
def tally_status() -> dict:
    """Check whether Tally Prime's XML API (port 9999) is reachable.

    ALWAYS call this FIRST before any other tool. If Tally is not running
    or the company is not loaded, all other tools will fail.
    Read-only GET -- safe to call any time.
    """
    return _client().check_connection()


# ---------------------------------------------------------------------------
# ledger tools
# ---------------------------------------------------------------------------



@mcp.tool()
def list_ledgers(company: str = "", preview: bool = False) -> dict:
    """List all ledgers with their parent group in the Tally company (read-only export).

    Safe to call any time -- it never modifies data. Useful for duplicate
    checks before creating ledgers, and for seeing which ledger sits under
    which group (Sundry Debtors, Bank Accounts, Duties & Taxes, etc.).

    Args:
        company: Company name. If empty, uses TALLY_COMPANY env var. Narrows the
            export to that company; avoids silent empty results.
        preview: True -> return the XML without sending anything to Tally.
    """
    return _client().list_ledgers(company=company or None, preview=preview)
@mcp.tool()
def create_party_ledger(
    name: str,
    party_type: str = "customer",
    alias: str = "",
    bill_wise: str = "Yes",
    credit_period: int = 0,
    credit_days_check: str = "",
    credit_limit: float = 0.0,
    has_credit_limit: bool = False,
    override_credit_limit: str = "",
    interest_on: str = "",
    interest_bill_wise: str = "",
    override_interest: str = "",
    override_adv_interest: str = "",
    pan: str = "",
    gstin: str = "",
    registration_type: str = "",
    place_of_supply: str = "",
    applicable_from: str = "",
    other_territory_assessee: str = "",
    ecomm_operator: str = "",
    ecomm_merchant_id: str = "",
    transporter: str = "",
    transporter_id: str = "",
    sez_party: str = "",
    address: str = "",
    state: str = "",
    country: str = "",
    pincode: str = "",
    opening_balance: float = 0.0,
    has_opening_balance: bool = False,
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a PARTY ledger (customer/supplier) in Tally Prime -- full party form.

    Dedicated party tool (like the bank-ledger tool): party_type auto-sets
    the parent; one call covers bill-wise + credit + interest + mailing +
    PAN/GSTIN + GST sub-screen flags.

    Tag mapping (basics 2026-09-30 live-verified; deep flags researched
    2026-09-30 via rtslink XML tags doc + CData Ledger object model):
      bill_wise            -> <ISBILLWISEON>
      credit_period        -> <BILLCREDITPERIOD> (days, number only)
      credit_days_check    -> <ISCREDITDAYSCHKON> ("Check for credit days...")
      credit_limit         -> <CREDITLIMIT> (amount, e.g. 50000.00)
      override_credit_limit-> <OVERRIDECREDITLIMIT>
      interest_on          -> <ISINTERESTON> (Activate interest calculation)
      interest_bill_wise   -> <INTERESTONBILLWISE>
      override_interest    -> <OVERRIDEINTEREST>
      override_adv_interest-> <OVERRIDEADVINTEREST>
      PAN                  -> <INCOMETAXNUMBER>
      GSTIN                -> nested <LEDGSTREGDETAILS.LIST>
      registration_type    -> <GSTREGISTRATIONTYPE> (Regular/Composition/
                               Consumer/Unregistered/Unknown/Deemed Exporter)
      other_territory_assessee -> <ISOTHTERRITORYASSESSEE>
      ecomm_operator       -> <ISECOMMOPERATOR> (+ ecomm_merchant_id)
      transporter          -> <ISTRANSPORTER> (+ transporter_id)
      sez_party            -> <ISSEZPARTY>
      Mailing              -> <LEDMAILINGDETAILS.LIST>
      Opening balance      -> <OPENINGBALANCE> signed: negative=Dr, positive=Cr

    NOTE: "Deemed Exporter" alag flag NAHI hai -- registration_type me
    "Deemed Exporter" bhejo. Interest RATE params aur party bank-details
    (beneficiary) nested lists v2 me aayenge (UI experiment pending).

    Args:
        name: Party ka naam, e.g. "Sharma Traders".
        party_type: "customer" -> Sundry Debtors; "supplier" -> Sundry Creditors.
        alias: Optional alias.
        bill_wise: "Yes"/"No" (default Yes).
        credit_period: Days, e.g. 30.
        credit_days_check: "Yes"/"No" -- voucher entry me credit days check.
        credit_limit: Amount; has_credit_limit=True ke sath bhejo.
        override_credit_limit: "Yes"/"No".
        interest_on / interest_bill_wise / override_interest /
            override_adv_interest: "Yes"/"No".
        pan: 10-char PAN.
        gstin: 15-char GSTIN.
        registration_type: e.g. "Regular".
        place_of_supply: State; defaults to `state` when GSTIN given.
        applicable_from: YYYYMMDD (example: "20260401"); for mailing use the
            books-begin date.
        other_territory_assessee / ecomm_operator / transporter / sez_party:
            "Yes"/"No" GST sub-screen flags.
        ecomm_merchant_id / transporter_id: text IDs.
        address/state/country/pincode: Mailing.
        opening_balance: Signed; negative=Dr, positive=Cr; has_opening_balance
            ke sath bhejo.
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    pt = party_type.strip().lower()
    parent = "Sundry Debtors" if pt == "customer" else "Sundry Creditors"
    client = _client()
    result = client.create_ledger(
        name,
        parent,
        alias=alias or None,
        bill_wise=bill_wise or None,
        credit_period=credit_period or None,
        credit_days_check=credit_days_check or None,
        credit_limit=credit_limit if has_credit_limit else None,
        override_credit_limit=override_credit_limit or None,
        interest_on=interest_on or None,
        interest_bill_wise=interest_bill_wise or None,
        override_interest=override_interest or None,
        override_adv_interest=override_adv_interest or None,
        pan=pan or None,
        gstin=gstin or None,
        registration_type=registration_type or None,
        place_of_supply=place_of_supply or None,
        applicable_from=applicable_from or None,
        other_territory_assessee=other_territory_assessee or None,
        ecomm_operator=ecomm_operator or None,
        ecomm_merchant_id=ecomm_merchant_id or None,
        transporter=transporter or None,
        transporter_id=transporter_id or None,
        sez_party=sez_party or None,
        address=address or None,
        state=state or None,
        country=country or None,
        pincode=pincode or None,
        opening_balance=opening_balance if has_opening_balance else None,
        preview=preview, confirm_token=confirm_token,
    )
    return result.to_dict()


@mcp.tool()
def alter_party_ledger(
    name: str,
    new_name: str = "",
    bill_wise: str = "",
    credit_period: int = 0,
    credit_days_check: str = "",
    credit_limit: float = 0.0,
    has_credit_limit: bool = False,
    override_credit_limit: str = "",
    interest_on: str = "",
    interest_bill_wise: str = "",
    override_interest: str = "",
    override_adv_interest: str = "",
    pan: str = "",
    gstin: str = "",
    registration_type: str = "",
    place_of_supply: str = "",
    applicable_from: str = "",
    other_territory_assessee: str = "",
    ecomm_operator: str = "",
    ecomm_merchant_id: str = "",
    transporter: str = "",
    transporter_id: str = "",
    sez_party: str = "",
    address: str = "",
    state: str = "",
    country: str = "",
    pincode: str = "",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Alter a PARTY ledger (customer/supplier) in Tally Prime -- full party form.

    Updates party fields on an existing ledger. Same field mapping as
    create_party_ledger. Always use preview=True first and confirm with user.

    IMPORTANT: Tally XML ALTER overwrites -- always send ALL fields you want
    to keep, not just the changed ones. Fetch current values first if needed.

    Args:
        name: Current exact name of the party ledger.
        new_name: New name for rename (empty = keep same).
        bill_wise: "Yes"/"No" -> <ISBILLWISEON>.
        credit_period: Days -> <BILLCREDITPERIOD>.
        credit_days_check: "Yes"/"No" -> <ISCREDITDAYSCHKON>.
        credit_limit: Amount; has_credit_limit=True ke sath bhejo.
        override_credit_limit: "Yes"/"No".
        interest_on / interest_bill_wise / override_interest /
            override_adv_interest: "Yes"/"No".
        pan: 10-char PAN -> <INCOMETAXNUMBER>.
        gstin: 15-char GSTIN -> nested <LEDGSTREGDETAILS.LIST>.
        registration_type: e.g. "Regular".
        place_of_supply: State; defaults to `state` when GSTIN given.
        applicable_from: YYYYMMDD (use books-begin date, e.g. "20260401").
        other_territory_assessee / ecomm_operator / transporter / sez_party:
            "Yes"/"No" GST sub-screen flags.
        ecomm_merchant_id / transporter_id: text IDs.
        address/state/country/pincode: Mailing (applicable_from=books-begin!).
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Required when preview=False.
    """
    client = _client()
    result = client.alter_ledger(
        name,
        new_name=new_name or None,
        bill_wise=bill_wise or None,
        credit_period=credit_period or None,
        credit_days_check=credit_days_check or None,
        credit_limit=credit_limit if has_credit_limit else None,
        override_credit_limit=override_credit_limit or None,
        interest_on=interest_on or None,
        interest_bill_wise=interest_bill_wise or None,
        override_interest=override_interest or None,
        override_adv_interest=override_adv_interest or None,
        pan=pan or None,
        gstin=gstin or None,
        registration_type=registration_type or None,
        place_of_supply=place_of_supply or None,
        applicable_from=applicable_from or None,
        other_territory_assessee=other_territory_assessee or None,
        ecomm_operator=ecomm_operator or None,
        ecomm_merchant_id=ecomm_merchant_id or None,
        transporter=transporter or None,
        transporter_id=transporter_id or None,
        sez_party=sez_party or None,
        address=address or None,
        state=state or None,
        country=country or None,
        pincode=pincode or None,
        preview=preview, confirm_token=confirm_token,
    )
    return result.to_dict()


@mcp.tool()
def create_bank_ledger(
    name: str,
    parent: str = "Bank Accounts",
    bank_name: str = "",
    holder_name: str = "",
    account_number: str = "",
    ifs_code: str = "",
    swift_code: str = "",
    branch: str = "",
    bsr_code: str = "",
    client_code: str = "",
    opening_balance: float = 0.0,
    has_opening_balance: bool = False,
    address: str = "",
    state: str = "",
    country: str = "",
    pincode: str = "",
    pan: str = "",
    gstin: str = "",
    registration_type: str = "",
    place_of_supply: str = "",
    applicable_from: str = "",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a BANK ledger in Tally Prime (dedicated bank-account tool).

    Same engine as create_ledger, sirf bank form ke fields -- agent ko
    generic ledger tool me bhatakna nahi padta.

    Verified tag mapping (UI-verified 2026-09-30 -- user ne form bhara,
    export se asli tags mile; PNB test ledger par dobara verified):
      bank_name      -> <BANKINGCONFIGBANK>  (e.g. "Punjab National Bank")
      holder_name    -> <BANKACCHOLDERNAME>   (A/c Holder's Name)
      account_number -> <BANKDETAILS>         (A/c No.)
      ifs_code       -> <IFSCODE>             (IFS Code)
      swift_code     -> <SWIFTCODE>            (SWIFT Code)
      branch         -> <BRANCHNAME>           (Branch)
      bsr_code       -> <BANKBSRCODE>          (export-only; main form par nahi)
      client_code    -> <CLIENTCODE>           (export-only; UI save ke baad gayab)
      PAN            -> <INCOMETAXNUMBER>
      GSTIN          -> nested <LEDGSTREGDETAILS.LIST> (APPLICABLEFROM +
                         PLACEOFSUPPLY ke sath)
      Mailing        -> <LEDMAILINGDETAILS.LIST>
      Opening balance-> <OPENINGBALANCE> signed: negative=Dr, positive=Cr

    Args:
        name: Ledger name, e.g. "PNB Bank A/c".
        parent: Parent group; default "Bank Accounts" ("Bank OD A/c" bhi chalega).
        bank_name: Bank ka naam, e.g. "Punjab National Bank".
        holder_name: A/c holder ka naam.
        account_number: Bank account number.
        ifs_code: IFSC, e.g. "HDFC0001234" (example only).
        swift_code: SWIFT, e.g. "HDFCINBBXXX" (example only).
        branch: Branch, e.g. "Andheri".
        bsr_code: BSR code (optional, export-only).
        client_code: Client code (optional, export-only).
        opening_balance: Signed amount; negative=Dr, positive=Cr (optional).
        has_opening_balance: Set True to actually send opening_balance.
        address/state/country/pincode: Mailing details (optional).
        pan: 10-char PAN (optional).
        gstin: 15-char GSTIN (optional).
        registration_type: e.g. "Regular" (optional).
        place_of_supply: State name; defaults to `state` when GSTIN is given.
        applicable_from: YYYYMMDD / YYYY-MM-DD / DD/MM/YYYY; defaults to today.
            For mailing, pass the books-begin date (example: "20260401").
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    client = _client()
    result = client.create_ledger(
        name,
        parent,
        bank_name=bank_name or None,
        holder_name=holder_name or None,
        account_number=account_number or None,
        ifs_code=ifs_code or None,
        swift_code=swift_code or None,
        branch=branch or None,
        bsr_code=bsr_code or None,
        client_code=client_code or None,
        opening_balance=opening_balance if has_opening_balance else None,
        address=address or None,
        state=state or None,
        country=country or None,
        pincode=pincode or None,
        pan=pan or None,
        gstin=gstin or None,
        registration_type=registration_type or None,
        place_of_supply=place_of_supply or None,
        applicable_from=applicable_from or None,
        preview=preview, confirm_token=confirm_token,
    )
    return result.to_dict()


@mcp.tool()
def create_cash_ledger(
    name: str,
    alias: str = "",
    currency: str = "",
    inventory_affected: str = "",
    cost_centres: str = "",
    interest_calculation: str = "",
    gst_applicable: str = "",
    behave_as_duties_taxes: str = "",
    opening_balance: float = 0.0,
    has_opening_balance: bool = False,
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a cash ledger under Cash-in-Hand with the FULL standard form.

    Cash-in-Hand uses Tally's standard ledger form (same as expenses/incomes
    groups) -- not just name + opening balance. Note: the main "Cash" ledger
    already exists by default in every company; use this for extra cash
    ledgers like "Petty Cash".

    Args:
        name: Cash ledger name, e.g. "Petty Cash".
        alias: Optional short name.
        currency: Currency of ledger (default = company base currency).
        inventory_affected: "Yes"/"No" -- Inventory values are affected.
        cost_centres: "Yes"/"No" -- Cost centres are applicable.
        interest_calculation: "Yes"/"No" -- Activate interest calculation
            (rate/slabs = deep config, not in this tool).
        gst_applicable: "Yes"/"No" -- GST applicability.
        behave_as_duties_taxes: "Yes"/"No" -- Behave as Duties & Taxes ledger.
        opening_balance: Opening balance amount (positive number).
        has_opening_balance: Set True to actually store the opening balance
            (otherwise it stays zero even if opening_balance is given).
        preview: If True, return the exact XML without sending to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    result = _client().create_ledger(
        name,
        "Cash-in-Hand",
        alias=alias or None,
        currency=currency or None,
        inventory_affected=inventory_affected or None,
        cost_centres=cost_centres or None,
        interest_on=interest_calculation or None,
        gst_applicable=gst_applicable or None,
        behave_as_duty=behave_as_duties_taxes or None,
        opening_balance=opening_balance,
        has_opening_balance=has_opening_balance,
        preview=preview, confirm_token=confirm_token,
    )
    return result.to_dict()


@mcp.tool()
def create_sales_purchase_ledger(
    name: str,
    ledger_type: str = "sales",
    alias: str = "",
    inventory_affected: str = "",
    gst_applicable: str = "",
    tcs_applicable: str = "",
    opening_balance: float = 0.0,
    has_opening_balance: bool = False,
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a sales or purchase ledger with the dedicated Sales/Purchase form.

    Args:
        name: Ledger name, e.g. "Sales A/c", "Purchase A/c".
        ledger_type: "sales" -> under Sales Accounts,
            "purchase" -> under Purchase Accounts.
        alias: Optional short name.
        inventory_affected: "Yes"/"No" -- Inventory values are affected.
        gst_applicable: "Yes"/"No" -- GST applicability (HSN/taxability/rate
            details are per-item deep config, not in this tool).
        tcs_applicable: "Yes"/"No" -- Is TCS applicable.
        opening_balance: Opening balance amount (positive number).
        has_opening_balance: Set True to actually store the opening balance
            (otherwise it stays zero even if opening_balance is given).
        preview: If True, return the exact XML without sending to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    lt = (ledger_type or "sales").strip().lower()
    parent = "Purchase Accounts" if lt == "purchase" else "Sales Accounts"
    result = _client().create_ledger(
        name,
        parent,
        alias=alias or None,
        inventory_affected=inventory_affected or None,
        gst_applicable=gst_applicable or None,
        tcs_applicable=tcs_applicable or None,
        opening_balance=opening_balance,
        has_opening_balance=has_opening_balance,
        preview=preview, confirm_token=confirm_token,
    )
    return result.to_dict()


@mcp.tool()
def create_expense_income_ledger(
    name: str,
    ledger_group: str = "indirect_expenses",
    alias: str = "",
    currency: str = "",
    inventory_affected: str = "",
    cost_centres: str = "",
    interest_calculation: str = "",
    gst_applicable: str = "",
    behave_as_duties_taxes: str = "",
    opening_balance: float = 0.0,
    has_opening_balance: bool = False,
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create an expense or income ledger with the FULL standard form.

    Direct/Indirect Expenses and Direct/Indirect Incomes use Tally's standard
    ledger form (same as Cash-in-Hand): currency, inventory, cost centres,
    interest, GST applicability, behave-as-duties-taxes, opening balance.

    Args:
        name: Ledger name, e.g. "Hammali", "Discount Received".
        ledger_group: one of "direct_expenses" -> Direct Expenses,
            "indirect_expenses" -> Indirect Expenses,
            "direct_incomes" -> Direct Incomes,
            "indirect_incomes" -> Indirect Incomes.
        alias: Optional short name.
        currency: Currency of ledger (default = company base currency).
        inventory_affected: "Yes"/"No" -- Inventory values are affected.
        cost_centres: "Yes"/"No" -- Cost centres are applicable.
        interest_calculation: "Yes"/"No" -- Activate interest calculation
            (rate/slabs = deep config, not in this tool).
        gst_applicable: "Yes"/"No" -- GST applicability.
        behave_as_duties_taxes: "Yes"/"No" -- Behave as Duties & Taxes ledger.
        opening_balance: Opening balance amount (positive number).
        has_opening_balance: Set True to actually store the opening balance
            (otherwise it stays zero even if opening_balance is given).
        preview: If True, return the exact XML without sending to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    g = (ledger_group or "indirect_expenses").strip().lower()
    group_map = {
        "direct_expenses": "Direct Expenses",
        "indirect_expenses": "Indirect Expenses",
        "direct_incomes": "Direct Incomes",
        "indirect_incomes": "Indirect Incomes",
    }
    parent = group_map.get(g, "Indirect Expenses")
    result = _client().create_ledger(
        name,
        parent,
        alias=alias or None,
        currency=currency or None,
        inventory_affected=inventory_affected or None,
        cost_centres=cost_centres or None,
        interest_on=interest_calculation or None,
        gst_applicable=gst_applicable or None,
        behave_as_duty=behave_as_duties_taxes or None,
        opening_balance=opening_balance,
        has_opening_balance=has_opening_balance,
        preview=preview, confirm_token=confirm_token,
    )
    return result.to_dict()


@mcp.tool()
def create_tax_ledger(
    name: str,
    duty_tax_type: str = "GST",
    duty_head: str = "",
    tax_percentage: float = 0.0,
    rounding_method: str = "",
    rounding_limit: float = 0.0,
    alias: str = "",
    opening_balance: float = 0.0,
    has_opening_balance: bool = False,
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a Duties & Taxes ledger with the dedicated tax form.

    CGST/SGST/IGST already exist (Tally auto-creates them) -- this tool
    REFUSES those names. Use it for other tax/duty ledgers (e.g. Cess).

    Args:
        name: Ledger name, e.g. "Kerala Flood Cess".
        duty_tax_type: Type of duty/tax, e.g. "GST".
        duty_head: Duty head, e.g. "Cess".
        tax_percentage: Percentage of calculation (usually 0 -- effective
            rate comes from voucher-level item/ledger).
        rounding_method: "Not Applicable" -- CAUTION (verified 2026-09-30):
            XML se SIRF 'Not Applicable' chipakta hai; Normal/Upward/Downward
            silently ignore hote hain (ADDLALLOCTYPE jaisa quirk). Asli value
            Tally UI se set karo.
        rounding_limit: Rounding limit amount.
        alias: Optional short name.
        opening_balance: Opening balance amount (positive number).
        has_opening_balance: Set True to actually store the opening balance.
        preview: If True, return the exact XML without sending to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    if name.strip().upper() in ("CGST", "SGST", "IGST"):
        return {
            "success": False, "action": "Create", "object_type": "Ledger",
            "name": name,
            "message": ("REFUSED: '%s' already exists -- Tally auto-creates "
                        "CGST/SGST/IGST. Manual create mat karo." % name.strip()),
        }
    rm = rounding_method.strip()
    result = _client().create_ledger(
        name,
        "Duties & Taxes",
        _allow_duties_taxes=True,
        alias=alias or None,
        tax_type=duty_tax_type or None,
        duty_head=duty_head or None,
        tax_percentage=tax_percentage,
        rounding_method=f"&#4; {rm}" if rm else None,
        rounding_limit=rounding_limit,
        opening_balance=opening_balance,
        has_opening_balance=has_opening_balance,
        preview=preview, confirm_token=confirm_token,
    )
    return result.to_dict()


@mcp.tool()
def create_standard_ledger(
    name: str,
    group: str,
    alias: str = "",
    currency: str = "",
    inventory_affected: str = "",
    cost_centres: str = "",
    interest_calculation: str = "",
    gst_applicable: str = "",
    behave_as_duties_taxes: str = "",
    opening_balance: float = 0.0,
    has_opening_balance: bool = False,
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a ledger under ANY standard-form group (the "extra" groups).

    Standard groups: Branch / Divisions, Capital Account, Cash-in-Hand,
    Current Assets, Current Liabilities, Deposits (Asset), Direct Expenses,
    Direct Incomes, Fixed Assets, Indirect Expenses, Indirect Incomes,
    Investments, Loans & Advances (Asset), Loans (Liability),
    Misc. Expenses (ASSET), Provisions, Reserves & Surplus, Secured Loans,
    Suspense A/c, Unsecured Loans.
    (Cash-in-Hand / Expenses / Incomes ke liye dedicated tools hain --
    ye baaki groups ke liye hai.)

    Args:
        name: Ledger name.
        group: Exact group name from the list above.
        alias: Optional short name.
        currency: Currency of ledger (default = company base currency).
        inventory_affected: "Yes"/"No" -- Inventory values are affected.
        cost_centres: "Yes"/"No" -- Cost centres are applicable.
        interest_calculation: "Yes"/"No" -- Activate interest calculation.
        gst_applicable: "Yes"/"No" -- GST applicability.
        behave_as_duties_taxes: "Yes"/"No" -- Behave as Duties & Taxes ledger.
        opening_balance: Opening balance amount (positive number).
        has_opening_balance: Set True to actually store the opening balance.
        preview: If True, return the exact XML without sending to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    valid = {
        "branch / divisions", "capital account", "cash-in-hand",
        "current assets", "current liabilities", "deposits (asset)",
        "direct expenses", "direct incomes", "fixed assets",
        "indirect expenses", "indirect incomes", "investments",
        "loans & advances (asset)", "loans (liability)",
        "misc. expenses (asset)", "provisions", "reserves & surplus",
        "secured loans", "suspense a/c", "unsecured loans",
    }
    g = (group or "").strip()
    if g.lower() not in valid:
        return {
            "success": False, "action": "Create", "object_type": "Ledger",
            "name": name,
            "message": (f"REFUSED: '{g}' standard group nahi hai. "
                        "Party/Bank/Sales/Purchase/Tax groups ke liye "
                        "dedicated tools use karo."),
        }
    # group ka sahi case wapas lao
    canon = {
        "branch / divisions": "Branch / Divisions",
        "capital account": "Capital Account",
        "cash-in-hand": "Cash-in-Hand",
        "current assets": "Current Assets",
        "current liabilities": "Current Liabilities",
        "deposits (asset)": "Deposits (Asset)",
        "direct expenses": "Direct Expenses",
        "direct incomes": "Direct Incomes",
        "fixed assets": "Fixed Assets",
        "indirect expenses": "Indirect Expenses",
        "indirect incomes": "Indirect Incomes",
        "investments": "Investments",
        "loans & advances (asset)": "Loans & Advances (Asset)",
        "loans (liability)": "Loans (Liability)",
        "misc. expenses (asset)": "Misc. Expenses (ASSET)",
        "provisions": "Provisions",
        "reserves & surplus": "Reserves & Surplus",
        "secured loans": "Secured Loans",
        "suspense a/c": "Suspense A/c",
        "unsecured loans": "Unsecured Loans",
    }[g.lower()]
    result = _client().create_ledger(
        name,
        canon,
        alias=alias or None,
        currency=currency or None,
        inventory_affected=inventory_affected or None,
        cost_centres=cost_centres or None,
        interest_on=interest_calculation or None,
        gst_applicable=gst_applicable or None,
        behave_as_duty=behave_as_duties_taxes or None,
        opening_balance=opening_balance,
        has_opening_balance=has_opening_balance,
        preview=preview, confirm_token=confirm_token,
    )
    return result.to_dict()


@mcp.tool()
def delete_ledger(name: str, preview: bool = True, confirm_token: str = "") -> dict:
    """Permanently DELETE a ledger from Tally Prime (uses UPPERCASE
    ACTION="Delete" via Import Data -- the verified true-delete pattern).

    This is irreversible. Always use preview=True first and confirm with the
    user before executing.

    Args:
        name: Exact name of the ledger to delete.
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().delete_ledger(name, preview=preview, confirm_token=confirm_token).to_dict()


@mcp.tool()
def alter_ledger(name: str, new_name: str = "", preview: bool = True, confirm_token: str = "") -> dict:
    """Alter/rename an existing ledger in Tally Prime.

    Always use preview=True first and confirm with the user.

    Args:
        name: Current exact name of the ledger.
        new_name: New name (empty = keep same, only update other fields).
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().alter_ledger(
        name, new_name=new_name or None, preview=preview, confirm_token=confirm_token).to_dict()


@mcp.tool()
def alter_vouchertype(name: str, new_name: str = "", preview: bool = True, confirm_token: str = "") -> dict:
    """Rename/alter a voucher type. Always preview first.

    Args:
        name: Current voucher type name.
        new_name: New name.
        preview: True -> XML preview only.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().alter_vouchertype(
        name, new_name=new_name or None, preview=preview, confirm_token=confirm_token).to_dict()


@mcp.tool()
def list_vouchertypes(company: str = "") -> dict:
    """List all voucher types (read-only)."""
    return _client().list_vouchertypes(company=company or None)


@mcp.tool()
def alter_godown(name: str, new_name: str = "", preview: bool = True, confirm_token: str = "") -> dict:
    """Rename/alter a godown. Always preview first.

    Args:
        name: Current godown name.
        new_name: New name.
        preview: True -> XML preview only.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().alter_godown(
        name, new_name=new_name or None, preview=preview, confirm_token=confirm_token).to_dict()


@mcp.tool()
def list_godowns(company: str = "") -> dict:
    """List all godowns (read-only)."""
    return _client().list_godowns(company=company or None)


@mcp.tool()
def alter_stockgroup(name: str, new_name: str = "", preview: bool = True, confirm_token: str = "") -> dict:
    """Rename/alter a stock group.

    Write safety: defaults to preview=True (returns the exact XML
    plus a single-use confirm_token, nothing sent). Execute with
    preview=False and that token; execution without a valid token
    is refused.
    """
    return _client().alter_stockgroup(
        name, new_name=new_name or None, preview=preview, confirm_token=confirm_token).to_dict()


@mcp.tool()
def alter_stockcategory(name: str, new_name: str = "", preview: bool = True, confirm_token: str = "") -> dict:
    """Rename/alter a stock category.

    Write safety: defaults to preview=True (returns the exact XML
    plus a single-use confirm_token, nothing sent). Execute with
    preview=False and that token; execution without a valid token
    is refused.
    """
    return _client().alter_stockcategory(
        name, new_name=new_name or None, preview=preview, confirm_token=confirm_token).to_dict()


@mcp.tool()
def list_units(company: str = "") -> dict:
    """List all units (read-only)."""
    return _client().list_units(company=company or None)




# ---------------------------------------------------------------------------
# group tools
# ---------------------------------------------------------------------------

@mcp.tool()
def create_primary_group(name: str, nature: str, alias: str = "",
                         sub_ledger: str = "", affects_gross_profit: str = "",
                         used_for_calculation: str = "", allocate_method: str = "",
                         is_addable: str = "", as_original: str = "",
                         affects_stock: str = "", preview: bool = True, confirm_token: str = "") -> dict:
    """Create a PRIMARY (autonomous, top-level) accounting group in Tally Prime.

    This is Tally's group form with "Under: Primary" - the full form showing
    "Nature of Group". The tool sends an empty <PARENT/> tag and sets the
    nature flags itself (writing <PARENT>Primary</PARENT> is rejected by Tally).

    nature (REQUIRED): "Assets" / "Liabilities" / "Income" / "Expenses".
      Sets ISREVENUE / AFFECTSGROSSPROFIT / ISDEEMEDPOSITIVE / AFFECTSSTOCK:
        Assets       -> No  / No  / Yes / No
        Liabilities  -> No  / No  / No  / No
        Income       -> Yes / Yes / No  / No
        Expenses     -> Yes / No  / Yes / No

    Optional form fields:
      sub_ledger: "Yes"/"No" - "Group behaves like a Sub-Ledger".
      affects_gross_profit: "Yes"/"No" - "Does it affect Gross Profits?".
        Overrides the nature default above.
      used_for_calculation: "Yes"/"No" - "Used for Calculation (taxes,
        discounts)?".
      allocate_method: "Method to Allocate when used in Purchase Invoice"
        ("Not Applicable" / "Appropriate by Qty" / "Appropriate by Value".
        Native format needs Tally's &#4; dropdown prefix - the tool adds it
        automatically; pass the friendly value only. NOTE (2026-09-30 live
        test): Tally silently ignores this via XML on primary groups -
        value stays "Not Applicable" even though import reports success.)
      affects_stock: "Yes"/"No" - overrides the nature default (No).
      is_addable / as_original: advanced native Tally flags ("Yes"/"No").
        Real tags, live-readable - but their exact mapping to Tally's
        "Nett Debit/Credit Balances for Reporting" field is NOT yet
        verified. Use preview=True and confirm in Tally's UI.
      alias: group alias. Verified working, but this company AVOIDS
        aliases (they clutter Tally's UI lists) - leave empty unless asked.

    Recommended workflow: tally_status first; never CREATE as an existence
    check; preview=True to inspect the exact XML when unsure.

    Args:
        name: New group name (must not already exist).
        nature: "Assets"/"Liabilities"/"Income"/"Expenses" (required).
        alias: Alias (optional; preferably left empty).
        sub_ledger: "Yes" or "No" (optional).
        affects_gross_profit: "Yes" or "No" (optional).
        used_for_calculation: "Yes" or "No" (optional).
        allocate_method: allocation method name (optional).
        is_addable: "Yes" or "No" (optional, advanced).
        as_original: "Yes" or "No" (optional, advanced).
        affects_stock: "Yes" or "No" (optional).
        preview: True -> return the exact XML without sending to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_group(
        name, "", mode="primary",
        nature=nature or None,
        alias=alias or None,
        sub_ledger=sub_ledger or None,
        affects_gross_profit=affects_gross_profit or None,
        used_for_calculation=used_for_calculation or None,
        allocate_method=allocate_method or None,
        is_addable=is_addable or None,
        as_original=as_original or None,
        affects_stock=affects_stock or None,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def create_group(name: str, parent: str, alias: str = "",
                 sub_ledger: str = "", affects_gross_profit: str = "",
                 used_for_calculation: str = "", allocate_method: str = "",
                 is_addable: str = "", as_original: str = "",
                 affects_stock: str = "", preview: bool = True, confirm_token: str = "") -> dict:
    """Create a SUB-GROUP under an existing/default group in Tally Prime.

    ("by default" wala - group kisi existing group ke andar banega.)
    parent (REQUIRED): existing group name - "Sundry Debtors", "Bank Accounts",
    "Current Assets", ... (groups nest - any group can have sub-groups).
    Nature is inherited from the parent (Tally's short form has no Nature
    field for sub-groups).

    Optional form fields:
      sub_ledger: "Yes"/"No" - "Group behaves like a Sub-Ledger".
      affects_gross_profit: "Yes"/"No" - "Does it affect Gross Profits?".
      used_for_calculation: "Yes"/"No" - "Used for Calculation (taxes,
        discounts)?".
      allocate_method: "Method to Allocate when used in Purchase Invoice"
        ("Not Applicable" / "Appropriate by Qty" / "Appropriate by Value".
        Native format needs Tally's &#4; dropdown prefix - the tool adds it
        automatically; pass the friendly value only.)
      affects_stock: "Yes"/"No".
      is_addable / as_original: advanced native Tally flags ("Yes"/"No").
        Real tags, live-readable - but their exact mapping to Tally's
        "Nett Debit/Credit Balances for Reporting" field is NOT yet
        verified. Use preview=True and confirm in Tally's UI.
      alias: group alias. Verified working, but this company AVOIDS
        aliases (they clutter Tally's UI lists) - leave empty unless asked.

    Recommended workflow: tally_status first; never CREATE as an existence
    check; preview=True to inspect the exact XML when unsure.

    Args:
        name: New group name (must not already exist).
        parent: Existing parent group (required).
        alias: Alias (optional; preferably left empty).
        sub_ledger: "Yes" or "No" (optional).
        affects_gross_profit: "Yes" or "No" (optional).
        used_for_calculation: "Yes" or "No" (optional).
        allocate_method: allocation method name (optional).
        is_addable: "Yes" or "No" (optional, advanced).
        as_original: "Yes" or "No" (optional, advanced).
        affects_stock: "Yes" or "No" (optional).
        preview: True -> return the exact XML without sending to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_group(
        name, parent, mode="subgroup",
        alias=alias or None,
        sub_ledger=sub_ledger or None,
        affects_gross_profit=affects_gross_profit or None,
        used_for_calculation=used_for_calculation or None,
        allocate_method=allocate_method or None,
        is_addable=is_addable or None,
        as_original=as_original or None,
        affects_stock=affects_stock or None,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def delete_group(name: str, preview: bool = True, confirm_token: str = "") -> dict:
    """Permanently DELETE an accounting group from Tally Prime.

    Irreversible. The group must be empty (no ledgers/sub-groups under it).
    Always use preview=True first and confirm with the user.

    Args:
        name: Exact name of the group to delete.
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().delete_group(name, preview=preview, confirm_token=confirm_token).to_dict()


@mcp.tool()
def alter_group(name: str, new_name: str = "", parent: str = "", nature: str = "",
                alias: str = "", sub_ledger: str = "", affects_gross_profit: str = "",
                used_for_calculation: str = "", is_addable: str = "",
                as_original: str = "", affects_stock: str = "",
                preview: bool = True,
                confirm_token: str = "") -> dict:
    """ALTER an existing accounting group in Tally Prime. Only the fields you
    pass are changed; everything else is left untouched.

    What can change:
      new_name: rename the group (old name finds it, new name replaces it).
      parent: move the group.
        - "" (omit) = don't touch the parent.
        - parent="Primary" = move to top-level (REQUIRES nature as well).
        - parent="<existing group>" = move under that group (nature is then
          ignored - a sub-group inherits nature from its parent).
      nature: re-classify a PRIMARY group ("Assets"/"Liabilities"/"Income"/
        "Expenses"); sets the nature flags (explicit affects_gross_profit /
        affects_stock override its defaults). Ignored with a warning on
        sub-groups.
      sub_ledger / affects_gross_profit / used_for_calculation / is_addable /
      as_original / affects_stock: "Yes"/"No" - same meaning as in the create
        tools; sent only when provided.
      alias: replace the alias (this company avoids aliases - leave empty
        unless asked).

    NOT supported: allocate_method ("Method to Allocate") - live-tested
    2026-09-30, Tally silently ignores ADDLALLOCTYPE via XML (import reports
    success but the value stays "Not Applicable"). Change it in Tally's UI.

    Safety: the tool starts with a READ-ONLY pre-check (group must exist, new
    name must be free, target parent must exist, a group can't be its own
    parent) and fails fast with a clear message. preview=True shows the exact
    XML (it still does the read-only lookup to resolve the current parent).
    WARNING: altering Tally's 28 default/reserved groups can break books -
    don't rename/reparent them.

    Args:
        name: Exact current name of the group (required).
        new_name: New name (optional).
        parent: "" = unchanged; "Primary" = make top-level; "<group>" = move
            under that group.
        nature: "Assets"/"Liabilities"/"Income"/"Expenses" (primary groups only).
        alias: Alias (optional; preferably left empty).
        sub_ledger: "Yes" or "No" (optional).
        affects_gross_profit: "Yes" or "No" (optional).
        used_for_calculation: "Yes" or "No" (optional).
        is_addable: "Yes" or "No" (optional, advanced).
        as_original: "Yes" or "No" (optional, advanced).
        affects_stock: "Yes" or "No" (optional).
        preview: True -> return the exact XML without sending the Alter.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().alter_group(
        name,
        new_name=new_name or None,
        parent=parent if parent else None,
        nature=nature or None,
        alias=alias or None,
        sub_ledger=sub_ledger or None,
        affects_gross_profit=affects_gross_profit or None,
        used_for_calculation=used_for_calculation or None,
        is_addable=is_addable or None,
        as_original=as_original or None,
        affects_stock=affects_stock or None,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def list_groups(parent: str = "") -> dict:
    """List accounting groups in Tally Prime (read-only, no changes).

    Returns {"groups": [{"name": ..., "parent": ...}], "count": N}, sorted by
    name. parent is "Primary" for top-level groups, else the parent group's
    name. Verified 2026-09-30.

    Args:
        parent: "" = all groups; "Primary" = top-level only;
            "<group name>" = sub-groups directly under that group.
    """
    return _client().list_groups(parent=parent)


# ---------------------------------------------------------------------------
# utility
# ---------------------------------------------------------------------------

@mcp.tool()
def create_vouchertype(name: str, parent: str, preview: bool = True, confirm_token: str = "") -> dict:
    """Create a voucher type in Tally Prime.

    `parent` MUST be an existing base voucher type (e.g. "Payment",
    "Receipt", "Journal", "Sales", "Purchase"). Verified 2026-09-28.

    Args:
        name: Voucher type name.
        parent: Existing base voucher type.
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_vouchertype(name, parent, preview=preview, confirm_token=confirm_token).to_dict()


@mcp.tool()
def delete_vouchertype(name: str, preview: bool = True, confirm_token: str = "") -> dict:
    """Permanently DELETE a voucher type from Tally Prime.

    Irreversible. Always use preview=True first and confirm with the user.

    Args:
        name: Exact name of the voucher type to delete.
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().delete_vouchertype(name, preview=preview, confirm_token=confirm_token).to_dict()


# ---------------------------------------------------------------------------
# stock group tools (verified 2026-09-28)
# ---------------------------------------------------------------------------

@mcp.tool()
def create_stockgroup(
    name: str,
    hsn_code: str = "",
    taxability: str = "",
    gst_rate: float = 0,
    applicable_from: str = "",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a stock group in Tally Prime, optionally with HSN + GST details.

    Verified tag mapping (do not change without re-verifying in Tally):
      - Top-level groups take NO <PARENT> ("Primary" is rejected by Tally).
      - <GSTAPPLICABLE> is NOT a Stock Group field -- never send it here.
      - The "Specific" UI option is stored as "Specify Details Here" in
        <SRCOFHSNDETAILS> / <SRCOFGSTDETAILS>; without these Tally keeps
        "As per Company/Stock Group".
      - taxability: "Taxable", "Exempt", "Nil Rated" or "Non-GST".
      - gst_rate: total GST % for Taxable groups (e.g. 5 -> CGST 2.5,
        SGST 2.5, IGST 5). Ignored unless taxability is "Taxable".

    Runs Create (basic) then Alter (HSN/GST) -- the verified working pattern.

    Args:
        name: Stock group name.
        hsn_code: e.g. "1006" (optional).
        taxability: "Taxable" / "Exempt" / "Nil Rated" / "Non-GST" (optional).
        gst_rate: Total GST % for Taxable groups, e.g. 5 (optional).
        applicable_from: YYYYMMDD / YYYY-MM-DD / DD/MM/YYYY; defaults to today.
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_stockgroup(
        name,
        hsn_code=hsn_code or None,
        taxability=taxability or None,
        gst_rate=gst_rate or None,
        applicable_from=applicable_from or None,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def delete_stockgroup(name: str, preview: bool = True, confirm_token: str = "") -> dict:
    """Permanently DELETE a stock group from Tally Prime.

    Irreversible. The group must be empty (no items/sub-groups under it).
    Always use preview=True first and confirm with the user.

    Args:
        name: Exact name of the stock group to delete.
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().delete_stockgroup(name, preview=preview, confirm_token=confirm_token).to_dict()


# ---------------------------------------------------------------------------
# stock category tools (verified 2026-09-28)
# ---------------------------------------------------------------------------

@mcp.tool()
def create_stockcategory(name: str, preview: bool = True, confirm_token: str = "") -> dict:
    """Create a stock category in Tally Prime.

    Verified simple format 2026-09-28:
      <STOCKCATEGORY Action="Create"><NAME>X</NAME></STOCKCATEGORY>
    Top-level categories take no parent.

    Args:
        name: Stock category name.
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_stockcategory(name, preview=preview, confirm_token=confirm_token).to_dict()


@mcp.tool()
def delete_stockcategory(name: str, preview: bool = True, confirm_token: str = "") -> dict:
    """Permanently DELETE a stock category from Tally Prime.

    Irreversible. The category must be empty (no items under it).
    Always use preview=True first and confirm with the user.

    Args:
        name: Exact name of the stock category to delete.
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().delete_stockcategory(name, preview=preview, confirm_token=confirm_token).to_dict()


# ---------------------------------------------------------------------------
# read-only export tools (verified 2026-09-28)
# ---------------------------------------------------------------------------

@mcp.tool()
def list_stockitems(company: str = "", preview: bool = False) -> dict:
    """List all stock item names in the Tally company (read-only export).

    Uses the verified Collection Export envelope. Safe to call any time --
    it never modifies data. Useful for duplicate checks before creating
    items, and for bill-to-Tally item matching.

    Args:
        company: Company name. If empty, uses TALLY_COMPANY env var. Narrows the
            export to that company; avoids silent empty results.
        preview: True -> return the XML without sending anything to Tally.
    """
    return _client().list_stockitems(company=company or None, preview=preview)


@mcp.tool()
def get_stock_summary(item_name: str = "", company: str = "") -> dict:
    """Show stock on hand per item (read-only).

    Verified 2026-10-03 against live Tally. Returns each stock item with
    its closing quantity (e.g. "8.90 Qtls"). Use before a sales entry to
    check availability -- never sell more than what's in hand.

    Args:
        item_name: Optional -- exact item name to check just one item.
            Empty returns all items.
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_stock_summary(
        item_name=item_name, company=company or None)


@mcp.tool()
def get_party_outstanding(party_name: str = "", company: str = "") -> dict:
    """Show party-wise outstanding payables/receivables (read-only).

    Verified 2026-10-04 against live Tally. Nets Day Book party entries
    by voucher type: Sales adds to receivable, Receipt reduces it;
    Purchase adds to payable, Payment reduces it.

    Args:
        party_name: Optional -- exact party ledger name for just one party.
            Empty returns all debtors and creditors.
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_party_outstanding(
        party_name=party_name, company=company or None)


# ---------------------------------------------------------------------------
# stock item tools (verified 2026-09-28)
# ---------------------------------------------------------------------------

@mcp.tool()
def create_stockitem(
    name: str,
    unit: str,
    group: str = "",
    category: str = "",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a stock item in Tally Prime.

    Workflow: create the stock GROUP first, then the item. The item's Under
    field defaults to Primary; pass group= to assign it at creation.
    Group name must match exactly (plain name, never the &#4; prefix).

    Args:
        name: Item name (e.g. "RICE BASMATI Noor 30kg").
        unit: Base unit symbol (e.g. "Qtls"). Must already exist.
        group: Stock group name to put the item under (optional).
        category: Stock category name (optional).
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_stockitem(
        name, unit, group=group or None, category=category or None,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def alter_stockitem(
    name: str,
    group: str = "",
    category: str = "",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Alter a stock item in Tally Prime (e.g. change its group/Under field).

    Use this when the user says "put this item under this group".
    Group name is sent as plain <PARENT> -- never the &#4; prefix.

    Args:
        name: Exact name of the existing stock item.
        group: New stock group name (Under field).
        category: New stock category name (optional).
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().alter_stockitem(
        name, group=group or None, category=category or None,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def delete_stockitem(name: str, preview: bool = True, confirm_token: str = "") -> dict:
    """Permanently DELETE a stock item from Tally Prime.

    Irreversible. Only works if the item has no transactions/vouchers.
    Always use preview=True first and confirm with the user.

    Args:
        name: Exact name of the stock item to delete.
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().delete_stockitem(name, preview=preview, confirm_token=confirm_token).to_dict()


# ---------------------------------------------------------------------------
# godown tools (verified 2026-09-29)
# ---------------------------------------------------------------------------

@mcp.tool()
def create_godown(
    name: str,
    parent: str = "",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a godown (warehouse/location) in Tally Prime.

    Prerequisite: "Maintain multiple godowns" must be ON in the company
    (F11 -> Inventory Features), otherwise only 'Main Location' exists.

    Args:
        name: Godown name (e.g. "Dudh Talai").
        parent: Parent godown name for sub-godowns (optional).
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_godown(
        name, parent=parent or None, preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def delete_godown(name: str, preview: bool = True, confirm_token: str = "") -> dict:
    """Permanently DELETE a godown from Tally Prime.

    Irreversible. Delete child godowns before parents. The godown must
    have no stock/vouchers. Always use preview=True first.

    Args:
        name: Exact name of the godown to delete.
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().delete_godown(name, preview=preview, confirm_token=confirm_token).to_dict()


# ---------------------------------------------------------------------------
# unit tools (verified 2026-10-03)
# ---------------------------------------------------------------------------

@mcp.tool()
def create_unit(name: str, formal_name: str = "", preview: bool = True, confirm_token: str = "") -> dict:
    """Create a simple unit of measure in Tally Prime (e.g. "kg", "Nos").

    Verified 2026-10-03 against live Tally. Uses the verified UNIT XML
    shape: <UNIT Action="Create"><NAME>X</NAME><ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>
    <FORMALNAME>Y</FORMALNAME></UNIT>.
    Required before a stock item can reference a unit that doesn't exist
    yet (fails with "Unit does not exist!" otherwise).

    Args:
        name: Unit name/symbol (e.g. "kg").
        formal_name: Full formal name (e.g. "Kilogram"). Recommended.
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_unit(name, formal_name=formal_name,
                                 preview=preview, confirm_token=confirm_token).to_dict()


@mcp.tool()
def alter_unit(name: str, formal_name: str = "", preview: bool = True, confirm_token: str = "") -> dict:
    """Alter a unit of measure in Tally Prime (e.g. set formal name).

    Args:
        name: Exact name of the unit to alter.
        formal_name: New formal name (e.g. "Kilogram").
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().alter_unit(name, formal_name=formal_name,
                                preview=preview, confirm_token=confirm_token).to_dict()


@mcp.tool()
def delete_unit(name: str, preview: bool = True, confirm_token: str = "") -> dict:
    """Permanently DELETE a unit of measure from Tally Prime.

    Irreversible. The unit must not be used by any stock item.
    Always use preview=True first.

    Args:
        name: Exact name of the unit to delete.
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().delete_unit(name, preview=preview, confirm_token=confirm_token).to_dict()


# ---------------------------------------------------------------------------
# purchase voucher tools (verified 2026-09-29) -- Phase 2 / Part 2 first tool!
# ---------------------------------------------------------------------------

@mcp.tool()
def create_purchase_voucher(
    date: str,
    party_name: str,
    items: list,
    charges: list = [],
    reference: str = "",
    narration: str = "",
    party_address_lines: list = [],
    party_state: str = "",
    party_country: str = "",
    party_pincode: str = "",
    party_gstin: str = "",
    voucher_number: str = "",
    remote_id: str = "",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a Purchase item-invoice voucher in Tally Prime.

    Verified 2026-09-29 against live Tally Prime (Your Company).
    Handles inventory items + extra charges (Insurance/Hammali/Freight)
    as separate voucher lines, with Party Details auto-populated.

    Verified tag mapping (do not change without re-verifying):
      - Items -> ALLINVENTORYENTRIES.LIST (negative amounts).
      - Charges -> voucher-level LEDGERENTRIES.LIST, minimal 3 tags only
        (LEDGERNAME + ISDEEMEDPOSITIVE=Yes + negative AMOUNT). NEVER put
        ISPARTYLEDGER on a charge -- Day Book shows the FIRST
        LEDGERENTRIES.LIST with ISPARTYLEDGER=Yes.
      - Supplier block FIRST with ISPARTYLEDGER=Yes, positive amount.
      - Party Details do NOT auto-fill on XML import -- this tool injects
        them explicitly (ADDRESS.LIST, STATENAME, COUNTRYOFRESIDENCE,
        PARTYGSTIN, etc.) as direct children of VOUCHER.
      - Voucher must net to zero: supplier = items + charges.

    Args:
        date: Voucher date YYYYMMDD (example: "20240401").
        party_name: Supplier ledger name (exact, must exist).
        items: List of {"name": item name, "qty": number, "unit": "Qtls",
            "rate": per-unit rate, "amount": line total,
            "godown": godown name (default "Main Location")}.
        charges: List of {"ledger": charge ledger name,
            "amount": positive number} e.g. [{"ledger": "Insurance",
            "amount": 415}]. Stored as negative debits.
        reference: Supplier bill/invoice number.
        narration: Voucher narration.
        party_address_lines: e.g. ["123, Industrial Area",
            "New Delhi - 110001"].
        party_state: e.g. "Haryana".
        party_country: e.g. "India".
        party_pincode: e.g. "110001".
        party_gstin: e.g. "29AABCU9603R1ZX".
        voucher_number: Voucher number hint (Tally may auto-number).
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_purchase_voucher(
        date=date,
        party_name=party_name,
        items=items,
        charges=charges or None,
        reference=reference,
        narration=narration,
        party_address_lines=party_address_lines or None,
        party_state=party_state,
        party_country=party_country,
        party_pincode=party_pincode,
        party_gstin=party_gstin,
        voucher_number=voucher_number,
        remote_id=remote_id,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def delete_voucher(
    voucher_type: str,
    remote_id: str,
    date: str = "",
    voucher_number: str = "",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Permanently DELETE a voucher from Tally Prime.

    Verified 2026-10-03: GST-TEST-003 (Purchase No. 13) -> DELETED=1, ERRORS=0.
    Uses UPPERCASE ACTION="Delete" via Import Data with REPORTNAME=Vouchers.

    remote_id is REQUIRED -- it's the only reliable identifier. Tool-created
    vouchers carry an explicit REMOTEID (example: "TXN-PUR-20260401-GST-TEST-003").
    UI-created vouchers get Tally-internal IDs that XML can't target --
    delete those in Tally's UI.

    Safety: the voucher is verified by REMOTEID in the Day Book of the given
    date (pinned company) before previewing, and re-checked before execution.
    'date' is therefore REQUIRED; without it the delete fails closed.

    Args:
        voucher_type: Voucher type, e.g. "Purchase", "Sales", "Payment", "Receipt".
        remote_id: Explicit REMOTEID of the voucher (find via Day Book export).
        date: Voucher date YYYYMMDD (example: "20260401"). REQUIRED for target
            verification.
        voucher_number: Voucher number as shown in Tally (e.g. "13").
        preview: If True, return the exact XML without sending to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().delete_voucher(
        voucher_type=voucher_type,
        remote_id=remote_id,
        date=date,
        voucher_number=voucher_number,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def create_sales_voucher(
    date: str,
    party_name: str,
    items: list,
    sale_type: str = "credit",
    charges: list = [],
    reference: str = "",
    narration: str = "",
    party_address_lines: list = [],
    party_state: str = "",
    party_country: str = "",
    party_pincode: str = "",
    party_gstin: str = "",
    voucher_number: str = "",
    remote_id: str = "",
    sales_ledger: str = "Sales A/c",
    buyer_name: str = "",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a Sales item-invoice voucher in Tally Prime (credit or cash).

    Research: SALES_VOUCHER_RESEARCH.md (2026-09-29) -- official TallyHelp
    sample + 7 sources cross-verified. Sales is the EXACT MIRROR of purchase.

    Verified tag mapping (do not change without re-verifying):
      - Customer block FIRST: LEDGERENTRIES.LIST with ISPARTYLEDGER=Yes,
        ISDEEMEDPOSITIVE=Yes, ISLASTDEEMEDPOSITIVE=Yes, NEGATIVE amount.
      - Credit sales: BILLALLOCATIONS.LIST (New Ref) on the customer entry.
        Cash sales: party = cash ledger, no bill allocations.
      - Charges/tax -> voucher-level LEDGERENTRIES.LIST, minimal 3 tags
        (LEDGERNAME + ISDEEMEDPOSITIVE=No + POSITIVE amount). NEVER put
        ISPARTYLEDGER on a charge.
      - Items -> ALLINVENTORYENTRIES.LIST with POSITIVE amounts,
        ISDEEMEDPOSITIVE=No, DESTINATIONGODOWNNAME (stock goes OUT),
        ACCOUNTINGALLOCATIONS -> sales ledger.
      - Party Details do NOT auto-fill on XML import -- injected explicitly.
      - Voucher must net to zero: customer(-) + items(+) + charges(+) = 0.

    Args:
        date: Voucher date YYYYMMDD (example: "20240401").
        party_name: Customer ledger name (credit) or cash ledger (cash sale).
        items: List of {"name": item name, "qty": number, "unit": "Qtls",
            "rate": per-unit rate, "amount": line total,
            "godown": godown name (default "Main Location")}.
        sale_type: "credit" (default) or "cash".
        charges: List of {"ledger": charge/tax ledger name,
            "amount": positive number} e.g. [{"ledger": "CGST", "amount": 25}].
            Stored as positive credits.
        reference: Invoice/bill number.
        narration: Voucher narration.
        party_address_lines: e.g. ["Shop 5, Main Road", "Mumbai - 400001"].
        party_state: e.g. "Madhya Pradesh".
        party_country: e.g. "India".
        party_pincode: e.g. "450001".
        party_gstin: e.g. "23ABCDE1234F1Z5".
        voucher_number: Voucher number hint (Tally may auto-number).
        remote_id: Explicit ID (auto: "TXN-SAL-<date>-<reference>").
        sales_ledger: Sales ledger name (default "Sales A/c").
        buyer_name: Cash sale me bill pe print hone wala buyer ka naam
            (e.g. "Walk-in Customer") -- ledger wahi rahega, naya ledger nahi banega.
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_sales_voucher(
        date=date,
        party_name=party_name,
        sale_type=sale_type,
        items=items,
        charges=charges or None,
        reference=reference,
        narration=narration,
        party_address_lines=party_address_lines or None,
        party_state=party_state,
        party_country=party_country,
        party_pincode=party_pincode,
        party_gstin=party_gstin,
        voucher_number=voucher_number,
        remote_id=remote_id,
        sales_ledger=sales_ledger,
        buyer_name=buyer_name,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def create_payment_voucher(
    date: str,
    debit_entries: list,
    credit_ledger: str = "Cash",
    reference: str = "",
    narration: str = "",
    voucher_number: str = "",
    remote_id: str = "",
    bank_transaction_type: str = "Others",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a Payment accounting voucher in Tally Prime (no inventory).

    Use for: supplier payments, expense payments, any cash/bank outflow.
    Verified 2026-10-03 against live Tally Prime (Your Company).

    Verified tag mapping (do not change without re-verifying):
      - VCHTYPE=Payment, OBJVIEW=Accounting Voucher View,
        PERSISTEDVIEW=Accounting Voucher View, EFFECTIVEDATE=DATE.
      - Debit entries: LEDGERNAME + ISDEEMEDPOSITIVE=Yes + NEGATIVE amount.
        Party payments: ISPARTYLEDGER=Yes + ISLASTDEEMEDPOSITIVE=Yes +
        BILLALLOCATIONS.LIST (NAME=bill ref, BILLTYPE=Agst Ref).
      - Credit entry (Cash/Bank): LEDGERNAME + ISDEEMEDPOSITIVE=No +
        POSITIVE amount. NO ISPARTYLEDGER.
      - Net rule: debits(-) + credit(+) = 0.
      - Sign rule (critical): DEEMED=Yes -> negative amount;
        DEEMED=No -> positive amount.

    Args:
        date: Voucher date YYYYMMDD (example: "20240401").
        debit_entries: List of {"ledger": ledger name, "amount": positive
            number, "bill_ref": bill number for Agst Ref (optional),
            "is_party": True/False (default True if bill_ref given)}.
            e.g. [{"ledger": "ABC Suppliers Pvt Ltd",
                   "amount": 5000, "bill_ref": "INV/2024/001"}]
        credit_ledger: Cash/Bank ledger name (default "Cash").
        reference: Payment reference/cheque number.
        narration: Voucher narration.
        voucher_number: Voucher number hint (Tally may auto-number).
        remote_id: Explicit ID (auto: "TXN-PAY-<date>-<reference>").
        bank_transaction_type: e.g. "Others", "Cheque" -- bank transaction
            type for BANKALLOCATIONS (optional).
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_payment_voucher(
        date=date,
        debit_entries=debit_entries,
        credit_ledger=credit_ledger,
        reference=reference,
        narration=narration,
        voucher_number=voucher_number,
        remote_id=remote_id,
        bank_transaction_type=bank_transaction_type,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def create_receipt_voucher(
    date: str,
    credit_entries: list,
    debit_ledger: str = "Cash",
    reference: str = "",
    narration: str = "",
    voucher_number: str = "",
    remote_id: str = "",
    bank_transaction_type: str = "Others",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a Receipt accounting voucher in Tally Prime (no inventory).

    Use for: customer receipts, income receipts, any cash/bank inflow.
    Mirror of payment. Verified 2026-10-03 (Your Company).

    Verified tag mapping (do not change without re-verifying):
      - VCHTYPE=Receipt, OBJVIEW=Accounting Voucher View,
        PERSISTEDVIEW=Accounting Voucher View, EFFECTIVEDATE=DATE.
      - Debit entry (Cash/Bank) FIRST: LEDGERNAME + ISDEEMEDPOSITIVE=Yes +
        NEGATIVE amount. NO ISPARTYLEDGER.
      - Credit entries (party/income): LEDGERNAME + ISDEEMEDPOSITIVE=No +
        POSITIVE amount. Party receipts: ISPARTYLEDGER=Yes +
        ISLASTDEEMEDPOSITIVE=Yes + BILLALLOCATIONS.LIST (NAME=bill ref,
        BILLTYPE=Agst Ref).
      - Net rule: debit(-) + credits(+) = 0.
      - Sign rule (critical): DEEMED=Yes -> negative amount;
        DEEMED=No -> positive amount.

    Args:
        date: Voucher date YYYYMMDD (example: "20240401").
        credit_entries: List of {"ledger": ledger name, "amount": positive
            number, "bill_ref": bill number for Agst Ref (optional),
            "is_party": True/False (default True if bill_ref given)}.
            e.g. [{"ledger": "Global Enterprises", "amount": 9020,
                   "bill_ref": "SALE-001"}]
        debit_ledger: Cash/Bank ledger name (default "Cash").
        reference: Receipt reference number.
        narration: Voucher narration.
        voucher_number: Voucher number hint (Tally may auto-number).
        remote_id: Explicit ID (auto: "TXN-RCT-<date>-<reference>").
        bank_transaction_type: e.g. "Others", "Cheque" -- bank transaction
            type for BANKALLOCATIONS (optional).
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_receipt_voucher(
        date=date,
        credit_entries=credit_entries,
        debit_ledger=debit_ledger,
        reference=reference,
        narration=narration,
        voucher_number=voucher_number,
        remote_id=remote_id,
        bank_transaction_type=bank_transaction_type,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def create_contra_voucher(
    date: str,
    debit_ledger: str,
    credit_ledger: str,
    amount: float,
    reference: str = "",
    narration: str = "",
    voucher_number: str = "",
    remote_id: str = "",
    bank_transaction_type: str = "Others",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a Contra voucher in Tally Prime (fund transfer, no inventory).

    Use for: cash deposited to bank, cash withdrawn from bank,
    bank-to-bank transfers. Verified 2026-10-04 (Your Company).

    Verified tag mapping (do not change without re-verifying):
      - VCHTYPE=Contra, OBJVIEW=Accounting Voucher View,
        PERSISTEDVIEW=Accounting Voucher View, EFFECTIVEDATE=DATE.
      - Debit entry (destination): LEDGERNAME + ISDEEMEDPOSITIVE=Yes +
        NEGATIVE amount. NO ISPARTYLEDGER.
      - Credit entry (source): LEDGERNAME + ISDEEMEDPOSITIVE=No +
        POSITIVE amount. NO ISPARTYLEDGER.
      - Net rule: debit(-) + credit(+) = 0.
      - Sign rule (critical): DEEMED=Yes -> negative amount;
        DEEMED=No -> positive amount.

    Args:
        date: Voucher date YYYYMMDD (example: "20240401").
        debit_ledger: Destination ledger where money goes TO
            (e.g. "PNB Bank A/c" for cash deposit).
        credit_ledger: Source ledger where money comes FROM
            (e.g. "Cash" for cash deposit).
        amount: Positive transfer amount (e.g. 10000).
        reference: Contra reference number.
        narration: Voucher narration.
        voucher_number: Voucher number hint (Tally may auto-number).
        remote_id: Explicit ID (auto: "TXN-CTR-<date>-<reference>").
        bank_transaction_type: e.g. "Others", "Cheque" -- bank transaction
            type for BANKALLOCATIONS (optional).
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_contra_voucher(
        date=date,
        debit_ledger=debit_ledger,
        credit_ledger=credit_ledger,
        amount=amount,
        reference=reference,
        narration=narration,
        voucher_number=voucher_number,
        remote_id=remote_id,
        bank_transaction_type=bank_transaction_type,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def create_journal_voucher(
    date: str,
    debit_entries: list,
    credit_entries: list,
    reference: str = "",
    narration: str = "",
    voucher_number: str = "",
    remote_id: str = "",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a Journal voucher in Tally Prime (adjustments, no cash/bank).

    Use for: depreciation, outstanding expenses, provisions,
    reclassifications, party-to-party transfers. NO cash/bank ledgers,
    NO inventory. Verified 2026-10-04 (Your Company).

    Verified tag mapping (do not change without re-verifying):
      - VCHTYPE=Journal, OBJVIEW=Accounting Voucher View,
        PERSISTEDVIEW=Accounting Voucher View, EFFECTIVEDATE=DATE,
        HASCASHFLOW=No.
      - Debit entry: LEDGERNAME + ISDEEMEDPOSITIVE=Yes +
        NEGATIVE amount.
      - Credit entry: LEDGERNAME + ISDEEMEDPOSITIVE=No +
        POSITIVE amount.
      - Net rule: signed amounts MUST net to 0 (balanced).
      - Sign rule (critical): DEEMED=Yes -> negative amount;
        DEEMED=No -> positive amount.
      - BILLALLOCATIONS supported per party ledger entry.

    Args:
        date: Voucher date YYYYMMDD (example: "20240401").
        debit_entries: List of {"ledger": str, "amount": float (positive),
            "bill_allocations": [{"bill_name": str,
            "bill_type": "New Ref"|"Agst Ref"|"On Account"}]}.
        credit_entries: Same shape as debit_entries.
        reference: Journal reference number.
        narration: Voucher narration.
        voucher_number: Voucher number hint (Tally may auto-number).
        remote_id: Explicit ID (auto: "TXN-JRN-<date>-<reference>").
        preview: True -> return the XML without sending anything to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_journal_voucher(
        date=date,
        debit_entries=debit_entries,
        credit_entries=credit_entries,
        reference=reference,
        narration=narration,
        voucher_number=voucher_number,
        remote_id=remote_id,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def create_debit_note(
    date: str,
    party_name: str,
    items: list,
    charges: list = None,
    reference: str = "",
    narration: str = "",
    voucher_number: str = "",
    remote_id: str = "",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a Debit Note in Tally Prime (purchase return, stock goes out).

    Use for: purchase returns, rate difference claims, post-purchase
    discounts from supplier. Debit Note = Purchase ka mirror.
    Verified 2026-10-04 via deep research (Your Company).

    Verified tag mapping:
      - VCHTYPE=Debit Note, OBJVIEW=Invoice Voucher View,
        VCHENTRYMODE=Item Invoice, ISINVOICE=Yes.
      - Supplier: ISDEEMEDPOSITIVE=Yes + NEGATIVE amount (debit).
      - Charges (GST reversal): ISDEEMEDPOSITIVE=No + POSITIVE amount.
      - Inventory: stock OUT (ISDEEMEDPOSITIVE=No, negative AMOUNT).

    Args:
        date: Voucher date YYYYMMDD.
        party_name: Supplier ledger name (exact).
        items: List of {name, qty, unit, rate, amount, godown}.
        charges: List of {ledger, amount} (e.g. CGST/SGST reversal).
        reference: Debit note reference number.
        narration: Voucher narration.
        voucher_number: Voucher number hint.
        remote_id: Explicit ID (auto: "TXN-DN-<date>-<reference>").
        preview: True -> return XML without sending to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_debit_note(
        date=date, party_name=party_name, items=items, charges=charges,
        reference=reference, narration=narration,
        voucher_number=voucher_number, remote_id=remote_id,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def create_credit_note(
    date: str,
    party_name: str,
    items: list,
    charges: list = None,
    reference: str = "",
    narration: str = "",
    voucher_number: str = "",
    remote_id: str = "",
    preview: bool = True,
    confirm_token: str = "",
) -> dict:
    """Create a Credit Note in Tally Prime (sales return, stock comes in).

    Use for: sales returns, post-sale discounts to customer,
    over-billing corrections. Credit Note = Sales ka mirror.
    Verified 2026-10-04 via deep research (Your Company).

    Verified tag mapping:
      - VCHTYPE=Credit Note, OBJVIEW=Invoice Voucher View,
        VCHENTRYMODE=Item Invoice, ISINVOICE=Yes.
      - Customer: ISDEEMEDPOSITIVE=No + POSITIVE amount (credit).
      - Charges (GST reversal): ISDEEMEDPOSITIVE=Yes + NEGATIVE amount.
      - Inventory: stock IN (ISDEEMEDPOSITIVE=Yes, positive AMOUNT).

    Args:
        date: Voucher date YYYYMMDD.
        party_name: Customer ledger name (exact).
        items: List of {name, qty, unit, rate, amount, godown}.
        charges: List of {ledger, amount} (e.g. CGST/SGST reversal).
        reference: Credit note reference number.
        narration: Voucher narration.
        voucher_number: Voucher number hint.
        remote_id: Explicit ID (auto: "TXN-CN-<date>-<reference>").
        preview: True -> return XML without sending to Tally.
        confirm_token: Single-use token from the preview of this exact call.
            Required when preview=False; execution without a valid token is refused.
    """
    return _client().create_credit_note(
        date=date, party_name=party_name, items=items, charges=charges,
        reference=reference, narration=narration,
        voucher_number=voucher_number, remote_id=remote_id,
        preview=preview, confirm_token=confirm_token,
    ).to_dict()


@mcp.tool()
def sync_masters(preview: bool = False) -> dict:
    """Sync all ledgers + stock items from Tally into local cache.

    Call at the START of each work session, or after creating masters
    in Tally UI. Saves to sync/masters.json with timestamp.

    The check tools (check_ledger_exists, check_stockitem_exists)
    auto-refresh if cache is older than 4 hours, so manual sync is
    mainly needed at session start.

    Args:
        preview: True -> show what would happen without syncing.
    """
    return _client().sync_masters(preview=preview)


@mcp.tool()
def check_ledger_exists(name: str, refresh: bool = False) -> dict:
    """Check if a ledger exists in Tally (any type: party, expense, etc.).

    Smart: auto-refreshes from Tally if cached data is older than 4 hours.
    Use before creating vouchers to avoid duplicate ledgers.

    Args:
        name: Ledger name to check (case-insensitive).
        refresh: True -> force fresh sync from Tally first.
    """
    return _client().check_ledger_exists(name=name, refresh=refresh)


@mcp.tool()
def check_stockitem_exists(name: str, refresh: bool = False) -> dict:
    """Check if a stock item exists in Tally.

    Smart: auto-refreshes from Tally if cached data is older than 4 hours.
    Use before creating purchase/sales vouchers to avoid errors.

    Args:
        name: Stock item name to check (case-insensitive).
        refresh: True -> force fresh sync from Tally first.
    """
    return _client().check_stockitem_exists(name=name, refresh=refresh)


@mcp.tool()
def get_day_book(
    date_from: str = "",
    date_to: str = "",
    voucher_type: str = "",
    company: str = "",
) -> dict:
    """Day Book -- all vouchers for a date range (read-only).

    Returns every voucher with date, type, number, reference, party,
    amount and narration. Verified 2026-10-04 (Your Company).

    Args:
        date_from: Start date YYYYMMDD (default: books begin).
        date_to: End date YYYYMMDD (default: today).
        voucher_type: Filter e.g. "Sales", "Purchase", "Payment", "Receipt",
            "Contra", "Journal", "Debit Note", "Credit Note".
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_day_book(
        date_from=date_from, date_to=date_to,
        voucher_type=voucher_type, company=company or None,
    )


@mcp.tool()
def get_ledger_report(
    ledger_name: str,
    date_from: str = "",
    date_to: str = "",
    company: str = "",
) -> dict:
    """Ledger Report -- all transactions for one ledger (read-only).

    Shows every voucher touching this ledger with debit/credit amounts,
    totals and closing balance. Verified 2026-10-04 (Your Company).

    Args:
        ledger_name: Exact ledger name (e.g. "ABC Suppliers Pvt Ltd").
        date_from: Start date YYYYMMDD (default: books begin).
        date_to: End date YYYYMMDD (default: today).
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_ledger_report(
        ledger_name=ledger_name, date_from=date_from, date_to=date_to,
        company=company or None,
    )


@mcp.tool()
def get_trial_balance(
    date_from: str = "",
    date_to: str = "",
    company: str = "",
) -> dict:
    """Trial Balance -- all ledgers with closing balances (read-only).

    Shows every ledger's debit/credit balance. Verified 2026-10-04
    (Your Company).

    Args:
        date_from: Start date YYYYMMDD (default: books begin).
        date_to: End date YYYYMMDD (default: today).
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_trial_balance(
        date_from=date_from, date_to=date_to, company=company or None,
    )


@mcp.tool()
def get_profit_loss(
    date_from: str = "",
    date_to: str = "",
    company: str = "",
) -> dict:
    """Profit & Loss -- income vs expenses (read-only).

    Shows P&L entries. Verified 2026-10-04 (Your Company).

    Args:
        date_from: Start date YYYYMMDD (default: books begin).
        date_to: End date YYYYMMDD (default: today).
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_profit_loss(
        date_from=date_from, date_to=date_to, company=company or None,
    )


@mcp.tool()
def get_balance_sheet(
    as_on: str = "",
    company: str = "",
) -> dict:
    """Balance Sheet -- assets vs liabilities as on a date (read-only).

    Verified 2026-10-04 (Your Company).

    Args:
        as_on: Date YYYYMMDD (default: today).
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_balance_sheet(as_on=as_on, company=company or None)


@mcp.tool()
def get_ageing_report(
    report_type: str = "receivable",
    date_from: str = "",
    date_to: str = "",
    company: str = "",
) -> dict:
    """Ageing Report -- party outstanding by age buckets (read-only).

    Buckets: 0-30, 30-60, 60-90, 90+ days. Verified 2026-10-04
    (Your Company).

    Args:
        report_type: "receivable" (customers) or "payable" (suppliers).
        date_from: Start date YYYYMMDD.
        date_to: End date YYYYMMDD.
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_ageing_report(
        report_type=report_type, date_from=date_from, date_to=date_to,
        company=company or None,
    )


@mcp.tool()
def get_godown_summary(
    godown_name: str = "",
    item_name: str = "",
    company: str = "",
) -> dict:
    """Godown Summary -- stock by godown/location (read-only).

    Shows qty per item in each godown. Verified 2026-10-04
    (Your Company).

    Args:
        godown_name: Filter by godown (e.g. "Main Location"). Empty = all godowns.
        item_name: Filter by stock item. Empty = all items.
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_godown_summary(
        godown_name=godown_name, item_name=item_name, company=company or None,
    )


@mcp.tool()
def get_gstr1(
    date_from: str = "",
    date_to: str = "",
    company: str = "",
) -> dict:
    """GSTR-1 -- GST sales return data (read-only).

    Reconstructs GSTR-1 tables (B2B/B2CL/B2CS/CDNR) from Sales/Credit Note
    vouchers. Verified 2026-10-05 via deep research (Your Company).

    Args:
        date_from: Start date YYYYMMDD (default: books begin).
        date_to: End date YYYYMMDD (default: today).
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_gstr1(
        date_from=date_from, date_to=date_to, company=company or None,
    )


@mcp.tool()
def get_gstr3b(
    date_from: str = "",
    date_to: str = "",
    company: str = "",
) -> dict:
    """GSTR-3B -- GST summary return data (read-only).

    Table 3.1 (outward supplies) and Table 4 (ITC) from vouchers.
    Verified 2026-10-05 via deep research (Your Company).

    Args:
        date_from: Start date YYYYMMDD (default: books begin).
        date_to: End date YYYYMMDD (default: today).
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_gstr3b(
        date_from=date_from, date_to=date_to, company=company or None,
    )


@mcp.tool()
def get_sales_register(
    date_from: str = "",
    date_to: str = "",
    company: str = "",
) -> dict:
    """Sales Register -- all sales vouchers with totals (read-only).

    Verified 2026-10-05 (Your Company).

    Args:
        date_from: Start date YYYYMMDD.
        date_to: End date YYYYMMDD.
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_sales_register(
        date_from=date_from, date_to=date_to, company=company or None,
    )


@mcp.tool()
def get_purchase_register(
    date_from: str = "",
    date_to: str = "",
    company: str = "",
) -> dict:
    """Purchase Register -- all purchase vouchers with totals (read-only).

    Verified 2026-10-05 (Your Company).

    Args:
        date_from: Start date YYYYMMDD.
        date_to: End date YYYYMMDD.
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_purchase_register(
        date_from=date_from, date_to=date_to, company=company or None,
    )


@mcp.tool()
def get_cash_flow(
    date_from: str = "",
    date_to: str = "",
    company: str = "",
) -> dict:
    """Cash Flow -- cash movement statement (read-only).

    Verified 2026-10-05 via deep research (Your Company).

    Args:
        date_from: Start date YYYYMMDD.
        date_to: End date YYYYMMDD.
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_cash_flow(
        date_from=date_from, date_to=date_to, company=company or None,
    )


@mcp.tool()
def get_payment_register(
    date_from: str = "",
    date_to: str = "",
    company: str = "",
) -> dict:
    """Payment Register -- all payment vouchers with totals (read-only).

    Verified 2026-10-05 (Your Company).

    Args:
        date_from: Start date YYYYMMDD.
        date_to: End date YYYYMMDD.
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_payment_register(
        date_from=date_from, date_to=date_to, company=company or None,
    )


@mcp.tool()
def get_receipt_register(
    date_from: str = "",
    date_to: str = "",
    company: str = "",
) -> dict:
    """Receipt Register -- all receipt vouchers with totals (read-only).

    Verified 2026-10-05 (Your Company).

    Args:
        date_from: Start date YYYYMMDD.
        date_to: End date YYYYMMDD.
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_receipt_register(
        date_from=date_from, date_to=date_to, company=company or None,
    )


@mcp.tool()
def get_bank_reconciliation(
    bank_ledger: str,
    date_from: str = "",
    date_to: str = "",
    company: str = "",
) -> dict:
    """Bank Reconciliation -- unreconciled bank entries (read-only).

    Shows which bank vouchers are reconciled (have bank date) vs not.
    Verified 2026-10-05 via deep research (Your Company).

    Args:
        bank_ledger: Bank ledger name (e.g. "PNB Bank A/c").
        date_from: Start date YYYYMMDD.
        date_to: End date YYYYMMDD.
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_bank_reconciliation(
        bank_ledger=bank_ledger, date_from=date_from, date_to=date_to,
        company=company or None,
    )


@mcp.tool()
def get_gstr9(
    date_from: str = "",
    date_to: str = "",
    company: str = "",
) -> dict:
    """GSTR-9 -- Annual GST return summary (read-only).

    Aggregates GSTR-1 and GSTR-3B data. V1: Table 4, 6-8, 9.
    Verified 2026-10-05 via deep research (Your Company).

    Args:
        date_from: Start date YYYYMMDD (e.g. year start).
        date_to: End date YYYYMMDD (e.g. year end).
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    return _client().get_gstr9(
        date_from=date_from, date_to=date_to, company=company or None,
    )


@mcp.tool()
def export_invoice_pdf(
    voucher_number: str,
    company: str = "",
) -> dict:
    """Export a sales voucher as PDF invoice (read-only + file output).

    Generates a professional GST invoice PDF from Tally voucher data.
    Returns the file path. Verified 2026-10-05.

    Args:
        voucher_number: Sales voucher number (e.g. "1").
        company: Company name. If empty, uses TALLY_COMPANY env var.
    """
    from tally_pdf import export_sales_invoice_pdf
    import re as _re
    import requests as _req

    comp = (company or "").strip()
    if not comp:
        try:
            comp = get_pinned_company()
        except CompanyNotConfiguredError as e:
            return {"success": False, "error": f"REFUSED: {e}"}
    client = _client()

    # Get full voucher details via XML collection
    env = (
        "<ENVELOPE><HEADER><VERSION>1</VERSION>"
        "<TALLYREQUEST>Export</TALLYREQUEST>"
        "<TYPE>Collection</TYPE><ID>Voucher Collection</ID></HEADER>"
        "<BODY><DESC><STATICVARIABLES>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVCURRENTCOMPANY>{xml_escape(comp)}</SVCURRENTCOMPANY>"
        "</STATICVARIABLES>"
        '<TDL><TDLMESSAGE><COLLECTION NAME="Voucher Collection">'
        "<TYPE>Voucher</TYPE>"
        "<FETCH>DATE, VOUCHERTYPENAME, VOUCHERNUMBER, PARTYLEDGERNAME, NARRATION</FETCH>"
        "<FETCH>AllInventoryEntries.STOCKITEMNAME</FETCH>"
        "<FETCH>AllInventoryEntries.ACTUALQTY</FETCH>"
        "<FETCH>AllInventoryEntries.RATE</FETCH>"
        "<FETCH>AllInventoryEntries.AMOUNT</FETCH>"
        "<FETCH>AllLedgerEntries.LEDGERNAME</FETCH>"
        "<FETCH>AllLedgerEntries.AMOUNT</FETCH>"
        "</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
    )
    try:
        _r = _req.post(client.base_url, data=env.encode("utf-8"),
                       headers={"Content-Type": "text/xml"}, timeout=30)
        _t = _r.content.decode("utf-8", "replace")
    except _req.RequestException as e:
        return {"success": False, "error": f"Fetch failed: {e}"}
    # Validate the response is well-formed XML with no DTD/entities before
    # extracting fields from it.
    try:
        _parse_xml_safe(_t)
    except (UnsafeXmlError, XmlParseError) as e:
        return {"success": False, "error": f"Bad voucher export response: {e}"}

    target = None
    for _vm in _re.finditer(r"<VOUCHER[\s>].*?</VOUCHER>", _t, _re.S):
        _v = _vm.group(0)
        _vtm = _re.search(r'VCHTYPE="([^"]+)"', _v[:500])
        _vtype = _vtm.group(1) if _vtm else ""
        _vnm = _re.search(r"<VOUCHERNUMBER[^>]*>([^<]*)</VOUCHERNUMBER>", _v)
        _vnum = _vnm.group(1).strip() if _vnm else ""
        if _vtype == "Sales" and _vnum == str(voucher_number):
            target = _v
            break
    if not target:
        return {"success": False,
                "error": f"Sales voucher '{voucher_number}' not found."}

    # Parse voucher details
    _dm = _re.search(r"<DATE[^>]*>([^<]+)</DATE>", target)
    _date = _dm.group(1).strip() if _dm else ""
    _plm = _re.search(r"<PARTYLEDGERNAME[^>]*>([^<]+)</PARTYLEDGERNAME>", target)
    _party = _plm.group(1).strip() if _plm else ""
    _nrm = _re.search(r"<NARRATION[^>]*>([^<]*)</NARRATION>", target)
    _narr = _nrm.group(1).strip() if _nrm else ""

    _items = []
    _total = 0.0
    for _im in _re.finditer(r"<ALLINVENTORYENTRIES\.LIST>(.*?)</ALLINVENTORYENTRIES\.LIST>", target, _re.S):
        _entry = _im.group(1)
        _snm = _re.search(r"<STOCKITEMNAME[^>]*>([^<]+)</STOCKITEMNAME>", _entry)
        _aqm = _re.search(r"<ACTUALQTY[^>]*>([^<]+)</ACTUALQTY>", _entry)
        _rtm = _re.search(r"<RATE[^>]*>([^<]+)</RATE>", _entry)
        _amm = _re.search(r"<AMOUNT[^>]*>([^<]+)</AMOUNT>", _entry)
        _iname = _snm.group(1).strip() if _snm else ""
        _iqty = _aqm.group(1).strip() if _aqm else ""
        _irate = _rtm.group(1).strip() if _rtm else ""
        _iamt = 0.0
        if _amm:
            try:
                _iamt = abs(float(_amm.group(1).strip()))
            except ValueError:
                pass
        if _iname:
            _items.append({"name": _iname, "qty": _iqty, "rate": _irate, "amount": _iamt})
            _total += _iamt

    # Get tax from ledger entries
    _cgst = _sgst = _igst = 0.0
    for _tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
        for _lm in _re.finditer(rf"<{_tag}>(.*?)</{_tag}>", target, _re.S):
            _body = _lm.group(1)
            _lnm = _re.search(r"<LEDGERNAME[^>]*>([^<]+)</LEDGERNAME>", _body)
            _amm = _re.search(r"<AMOUNT[^>]*>([^<]+)</AMOUNT>", _body)
            if not _lnm or not _amm:
                continue
            _lname = _lnm.group(1).strip().lower()
            try:
                _amt = abs(float(_amm.group(1).strip()))
            except ValueError:
                continue
            if "cgst" in _lname:
                _cgst += _amt
            elif "sgst" in _lname:
                _sgst += _amt
            elif "igst" in _lname:
                _igst += _amt
    _total += _cgst + _sgst + _igst

    voucher_data = {
        "voucher_number": voucher_number,
        "date": _date,
        "party": _party,
        "items": _items if _items else [{"name": "As per bill", "qty": "", "rate": "", "amount": _total}],
        "cgst": _cgst, "sgst": _sgst, "igst": _igst,
        "total": _total,
        "narration": _narr,
    }

    try:
        output_path = _resolve_invoice_path(voucher_number)
    except ValueError as e:
        return {"success": False, "error": f"REFUSED: {e}"}
    try:
        export_sales_invoice_pdf(voucher_data, output_path)
        return {"success": True, "file": output_path,
                "message": f"Invoice PDF saved."}
    except (OSError, ValueError) as e:
        return {"success": False, "error": f"PDF export failed: {e}"}


if __name__ == "__main__":
    mcp.run()
