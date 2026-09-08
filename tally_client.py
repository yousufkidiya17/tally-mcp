"""Tally Prime XML API client (HTTP port 9999).

Verified against live Tally Prime on 2026-09-28. Findings baked in:

- PAN  -> <INCOMETAXNUMBER>               (PANNUMBER / INCOMETAXNO / PAN do NOT work)
- GSTIN -> nested <LEDGSTREGDETAILS.LIST> with APPLICABLEFROM + PLACEOFSUPPLY
           (flat <GSTIN> tag and <PARTYGSTIN> do NOT populate the UI field;
            PARTYGSTIN stores a value but stays invisible in Tally)
- GST registration type -> flat <GSTREGISTRATIONTYPE> (e.g. Regular)
- Mailing details -> <LEDMAILINGDETAILS.LIST> with APPLICABLEFROM
- Group PARENT must be an existing group; "Primary" is rejected by Tally
- Delete needs UPPERCASE ACTION="Delete" sent via "Import Data"
- NEVER trust HTTP 200 alone: always parse CREATED / ALTERED / DELETED /
  ERRORS / LINEERROR from the response body.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import secrets
import threading
import time as _time_mod
import xml.etree.ElementTree as _ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional
from xml.sax.saxutils import escape as _xml_escape

import requests

DEFAULT_TALLY_URL = os.environ.get("TALLY_URL", "http://localhost:9999/")
DEFAULT_TIMEOUT = 30

_IMPORT_ENVELOPE = (
    "<ENVELOPE>"
    "<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>"
    "<BODY><IMPORTDATA>"
    "<REQUESTDESC><REPORTNAME>All Masters</REPORTNAME>"
    "<STATICVARIABLES><SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY></STATICVARIABLES>"
    "</REQUESTDESC>"
    "<REQUESTDATA>"
    '<TALLYMESSAGE xmlns:UDF="TallyUDF">{body}</TALLYMESSAGE>'
    "</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
)

# Voucher delete needs REPORTNAME=Vouchers (verified 2026-10-03:
# GST-TEST-003 delete -> DELETED=1 with this envelope).
_VOUCHER_ENVELOPE = (
    "<ENVELOPE>"
    "<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>"
    "<BODY><IMPORTDATA>"
    "<REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME>"
    "<STATICVARIABLES><SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>"
    "</STATICVARIABLES></REQUESTDESC>"
    "<REQUESTDATA>"
    '<TALLYMESSAGE xmlns:UDF="TallyUDF">{body}</TALLYMESSAGE>'
    "</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_PREFIX = os.environ.get("TALLY_PREFIX", "TXN")


class CompanyNotConfiguredError(Exception):
    """Raised when a mutation is attempted without TALLY_COMPANY set."""


class MutationTokenError(Exception):
    """Raised when a confirm_token is missing, invalid, expired or reused."""


class UnsafeXmlError(ValueError):
    """Raised when a Tally response contains DTD/entity declarations."""


class XmlParseError(ValueError):
    """Raised when a Tally response is not well-formed XML."""


class ImbalanceError(ValueError):
    """Raised when voucher debit/credit entries do not balance."""


def get_pinned_company() -> str:
    """Return the validated TALLY_COMPANY all mutations are pinned to.

    Fails closed: empty, unset or whitespace-only configuration raises
    CompanyNotConfiguredError instead of silently targeting whichever
    company happens to be open in Tally.
    """
    company = (os.environ.get("TALLY_COMPANY", "") or "").strip()
    if not company:
        raise CompanyNotConfiguredError(
            "TALLY_COMPANY is not set. Set the TALLY_COMPANY environment "
            "variable to the exact Tally company name before any "
            "create/alter/delete operation. Refusing to guess the company."
        )
    return company

def xml_escape(value: str) -> str:
    """Escape text for inclusion in Tally XML."""
    return _xml_escape(value, {'"': "&quot;"})


def xml_escape_keep_marker(value: str) -> str:
    """Like xml_escape, but preserves Tally's &#4; list-selection markers
    (numeric character references) instead of double-escaping them."""
    return xml_escape(value).replace("&amp;#", "&#")


def yyyymmdd(value: Optional[str] = None) -> str:
    """Normalise a date to Tally's YYYYMMDD.

    Accepts YYYYMMDD, YYYY-MM-DD or DD/MM/YYYY. Defaults to today.
    """
    if not value:
        return date.today().strftime("%Y%m%d")
    v = value.strip()
    if re.fullmatch(r"\d{8}", v):
        return v
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        return "".join(m.groups())
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", v)
    if m:
        d, mo, y = m.groups()
        return f"{y}{mo}{d}"
    raise ValueError(
        f"Unrecognised date format: {value!r} (use YYYYMMDD, YYYY-MM-DD or DD/MM/YYYY)"
    )


def _pan_warning(pan: str) -> Optional[str]:
    if not re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", pan.strip().upper()):
        return f"PAN '{pan}' does not look like a valid 10-character PAN (AAAAA9999A)."
    return None


def _gstin_warning(gstin: str) -> Optional[str]:
    if not re.fullmatch(r"[0-9A-Z]{15}", gstin.strip().upper()):
        return f"GSTIN '{gstin}' does not look like a valid 15-character GSTIN."
    return None


# ---------------------------------------------------------------------------
# XML builders (verified tag shapes)
# ---------------------------------------------------------------------------

def build_ledger_message(
    name: str,
    action: str,  # "Create" | "Alter" | "Delete"
    parent: Optional[str] = None,
    alias: Optional[str] = None,
    remove_alias: bool = False,              # True -> NAME.LIST me sirf asli naam (alias hat jayega)
    bill_wise: Optional[str] = None,          # "Yes"/"No" -> <ISBILLWISEON>
    credit_period: Optional[int] = None,      # days, number only -> <BILLCREDITPERIOD>
    credit_days_check: Optional[str] = None,  # "Yes"/"No" -> <ISCREDITDAYSCHKON> (confirmed 2026-09-30: CData object model)
    # -- party DEEP fields (researched 2026-09-30: rtslink Tally9 XML tags doc +
    # CData Ledger object-model dump; flat tags, same import pattern as basics) --
    interest_on: Optional[str] = None,            # "Yes"/"No" -> <ISINTERESTON> (Activate interest calculation)
    interest_bill_wise: Optional[str] = None,     # "Yes"/"No" -> <INTERESTONBILLWISE> (Calculate Interest Transaction-by-transaction)
    override_interest: Optional[str] = None,      # "Yes"/"No" -> <OVERRIDEINTEREST> (Over-ride Parameters for each transaction)
    override_adv_interest: Optional[str] = None,  # "Yes"/"No" -> <OVERRIDEADVINTEREST> (advanced override)
    other_territory_assessee: Optional[str] = None,  # "Yes"/"No" -> <ISOTHTERRITORYASSESSEE> (Assessee of Other Territory)
    ecomm_operator: Optional[str] = None,         # "Yes"/"No" -> <ISECOMMOPERATOR> (Is e-commerce operator)
    ecomm_merchant_id: Optional[str] = None,      # -> <ECOMMMERCHANTID>
    transporter: Optional[str] = None,           # "Yes"/"No" -> <ISTRANSPORTER> (Is the Party a Transporter)
    transporter_id: Optional[str] = None,        # -> <TRANSPORTERID>
    sez_party: Optional[str] = None,             # "Yes"/"No" -> <ISSEZPARTY>
    # NOTE (research 2026-09-30): "Deemed Exporter" is NOT a separate flag in
    # TallyPrime 3.0+ -- use registration_type="Deemed Exporter" (GSTREGISTRATIONTYPE).
    # Interest RATE params + party bank-details (beneficiary) nested lists are
    # NOT on the ledger object -- deferred to v2 (UI-set-then-export experiment).
    opening_balance: Optional[float] = None,  # signed: negative=Dr, positive=Cr
    address: Optional[str] = None,
    state: Optional[str] = None,
    country: Optional[str] = None,
    pincode: Optional[str] = None,
    pan: Optional[str] = None,
    gstin: Optional[str] = None,
    registration_type: Optional[str] = None,
    place_of_supply: Optional[str] = None,
    applicable_from: Optional[str] = None,
    # -- basic per-form fields (added 2026-09-30; scope = BASIC only) --
    credit_limit: Optional[float] = None,        # -> <CREDITLIMIT>
    override_credit_limit: Optional[str] = None,  # "Yes"/"No" -> <OVERRIDECREDITLIMIT>
    inventory_affected: Optional[str] = None,    # "Yes"/"No" -> <AFFECTSSTOCK>
    currency: Optional[str] = None,              # -> <CURRENCYNAME>
    cost_centres: Optional[str] = None,          # "Yes"/"No" -> <ISCOSTCENTRESON>
    bank_name: Optional[str] = None,              # -> <BANKINGCONFIGBANK> (UI-verified 2026-09-30: screenshot)
    holder_name: Optional[str] = None,           # -> <BANKACCHOLDERNAME> (UI-verified 2026-09-30: screenshot)
    # UI-VERIFIED 2026-09-30 (user ne form bhara, export se asli tags mile):
    account_number: Optional[str] = None,       # -> <BANKDETAILS> (A/c No.)
    ifs_code: Optional[str] = None,              # -> <IFSCODE> (IFS Code)
    swift_code: Optional[str] = None,            # -> <SWIFTCODE> (SWIFT Code)
    branch: Optional[str] = None,                # -> <BRANCHNAME> (Branch)
    # Sub-screen wale ("Set/Alter Bank configuration"): export me store hote hain,
    # main form par nahi dikhte. CLIENTCODE to UI save ke baad export se GAYAB ho gaya!
    bsr_code: Optional[str] = None,              # -> <BANKBSRCODE> (export-only)
    client_code: Optional[str] = None,           # -> <CLIENTCODE> (export-only; UI save hata deta hai)
    gst_applicable: Optional[str] = None,        # "Yes"/"No" -> <GSTAPPLICABLE>&#4; Applicable/Not Applicable</GSTAPPLICABLE>
    tcs_applicable: Optional[str] = None,         # "Yes"/"No" -> <ISTCSAPPLICABLE>
    # -- tax form: ALTER-ONLY (create_ledger refuses Duties & Taxes) --
    tax_type: Optional[str] = None,              # -> <TAXTYPE> e.g. GST
    duty_head: Optional[str] = None,             # -> <GSTDUTYHEAD> e.g. CGST
    gst_type: Optional[str] = None,              # -> <GSTTYPE> (SGST anomaly: verify live)
    tax_percentage: Optional[float] = None,      # -> <RATEOFTAXCALCULATION>
    rounding_method: Optional[str] = None,       # -> <ROUNDINGMETHOD> (&#4; format)
    rounding_limit: Optional[float] = None,      # -> <ROUNDINGLIMIT>
    behave_as_duty: Optional[str] = None,        # "Yes"/"No" -> <ISBEHAVEASDUTY>
) -> str:
    """Build the <LEDGER ...> TALLYMESSAGE body for Create / Alter / Delete."""
    n = xml_escape(name.strip())
    parts = [f'<LEDGER NAME="{n}" ACTION="{action}">', f"<NAME>{n}</NAME>"]

    if action == "Delete":
        parts.append("</LEDGER>")
        return "".join(parts)

    if parent:
        parts.append(f"<PARENT>{xml_escape(parent.strip())}</PARENT>")

    if alias:
        # VERIFIED pattern 2026-09-30 (noumaanahamed/tally-prime-api-docs):
        # extra <NAME> entries in NAME.LIST become aliases.
        parts.append("<NAME.LIST>")
        parts.append(f"<NAME>{n}</NAME>")
        parts.append(f"<NAME>{xml_escape(alias.strip())}</NAME>")
        parts.append("</NAME.LIST>")
    elif remove_alias and action == "Alter":
        # Alias hatane ke liye: NAME.LIST me SIRF asli naam bhejo.
        # (alias='' bhejne se tag hi nahi jata -- Tally chhuta nahi! verified 2026-09-30)
        parts.append("<NAME.LIST>")
        parts.append(f"<NAME>{n}</NAME>")
        parts.append("</NAME.LIST>")

    if bill_wise:
        # VERIFIED 2026-09-30 (pnpc live-tested template + Tally tag list)
        v = bill_wise.strip().capitalize()
        parts.append(f"<ISBILLWISEON>{'Yes' if v == 'Yes' else 'No'}</ISBILLWISEON>")

    if credit_period is not None:
        # VERIFIED 2026-09-30: number of days ONLY (not "30 Days")
        parts.append(f"<BILLCREDITPERIOD>{int(credit_period)}</BILLCREDITPERIOD>")

    if credit_days_check:
        # CONFIRMED 2026-09-30 (CData Ledger object model: IsCreditDaysChkOn)
        v = credit_days_check.strip().capitalize()
        parts.append(f"<ISCREDITDAYSCHKON>{'Yes' if v == 'Yes' else 'No'}</ISCREDITDAYSCHKON>")

    # -- party deep Yes/No flags (researched 2026-09-30; flat tags) --
    _yn = lambda v: "Yes" if v.strip().capitalize() == "Yes" else "No"
    for param_val, tag in (
        (interest_on, "ISINTERESTON"),
        (interest_bill_wise, "INTERESTONBILLWISE"),
        (override_interest, "OVERRIDEINTEREST"),
        (override_adv_interest, "OVERRIDEADVINTEREST"),
        (other_territory_assessee, "ISOTHTERRITORYASSESSEE"),
        (ecomm_operator, "ISECOMMOPERATOR"),
        (transporter, "ISTRANSPORTER"),
        (sez_party, "ISSEZPARTY"),
    ):
        if param_val:
            parts.append(f"<{tag}>{_yn(param_val)}</{tag}>")
    if ecomm_merchant_id:
        parts.append(f"<ECOMMMERCHANTID>{xml_escape(ecomm_merchant_id.strip())}</ECOMMMERCHANTID>")
    if transporter_id:
        parts.append(f"<TRANSPORTERID>{xml_escape(transporter_id.strip())}</TRANSPORTERID>")

    if opening_balance is not None:
        # VERIFIED 2026-09-30 (frappe/tally_migrator real-export analysis +
        # rtslink udiMagic import formula): negative = Dr, positive = Cr.
        parts.append(f"<OPENINGBALANCE>{float(opening_balance)}</OPENINGBALANCE>")

    if any([address, state, country, pincode]):
        parts.append("<LEDMAILINGDETAILS.LIST>")
        parts.append(f"<APPLICABLEFROM>{yyyymmdd(applicable_from)}</APPLICABLEFROM>")
        if address:
            parts.append(f"<ADDRESS>{xml_escape(address.strip())}</ADDRESS>")
        if state:
            parts.append(f"<STATE>{xml_escape(state.strip())}</STATE>")
        if country:
            parts.append(f"<COUNTRY>{xml_escape(country.strip())}</COUNTRY>")
        if pincode:
            parts.append(f"<PINCODE>{xml_escape(pincode.strip())}</PINCODE>")
        parts.append("</LEDMAILINGDETAILS.LIST>")

    if pan:
        # VERIFIED: INCOMETAXNUMBER is the working tag (not PANNUMBER etc.)
        parts.append(f"<INCOMETAXNUMBER>{xml_escape(pan.strip().upper())}</INCOMETAXNUMBER>")

    if registration_type:
        parts.append(f"<GSTREGISTRATIONTYPE>{xml_escape(registration_type.strip())}</GSTREGISTRATIONTYPE>")

    if gstin:
        # VERIFIED: GSTIN only sticks via this nested list with APPLICABLEFROM
        # + PLACEOFSUPPLY. Flat <GSTIN> / <PARTYGSTIN> do not show in the UI.
        parts.append("<LEDGSTREGDETAILS.LIST>")
        parts.append(f"<APPLICABLEFROM>{yyyymmdd(applicable_from)}</APPLICABLEFROM>")
        pos = (place_of_supply or state or "").strip()
        parts.append(f"<PLACEOFSUPPLY>{xml_escape(pos)}</PLACEOFSUPPLY>")
        parts.append(f"<GSTIN>{xml_escape(gstin.strip().upper())}</GSTIN>")
        parts.append("</LEDGSTREGDETAILS.LIST>")

    # ---- basic per-form fields (2026-09-30; scope = BASIC only) ----
    if credit_limit is not None:
        # Tag CONFIRMED (live export shows <CREDITLIMIT/>); amount format 50000.00.
        parts.append(f"<CREDITLIMIT>{float(credit_limit):.2f}</CREDITLIMIT>")

    if override_credit_limit:
        # CONFIRMED via official import mapping (alias "Override Credit Limit").
        v = override_credit_limit.strip().capitalize()
        parts.append(f"<OVERRIDECREDITLIMIT>{'Yes' if v == 'Yes' else 'No'}</OVERRIDECREDITLIMIT>")

    if inventory_affected:
        # VERIFIED live export: <AFFECTSSTOCK>Yes</AFFECTSSTOCK> on Purchase A/c.
        v = inventory_affected.strip().capitalize()
        parts.append(f"<AFFECTSSTOCK>{'Yes' if v == 'Yes' else 'No'}</AFFECTSSTOCK>")

    if currency:
        # Tag VERIFIED (Cash ledger shows <CURRENCYNAME>?</CURRENCYNAME> = base).
        parts.append(f"<CURRENCYNAME>{xml_escape(currency.strip())}</CURRENCYNAME>")

    if cost_centres:
        # VERIFIED live export: <ISCOSTCENTRESON>No</ISCOSTCENTRESON>.
        v = cost_centres.strip().capitalize()
        parts.append(f"<ISCOSTCENTRESON>{'Yes' if v == 'Yes' else 'No'}</ISCOSTCENTRESON>")

    # ---- bank form (basic; UI-verified 2026-09-30: user ne form bhara, export se tags mile) ----
    if bank_name:
        parts.append(f"<BANKINGCONFIGBANK>{xml_escape(bank_name.strip())}</BANKINGCONFIGBANK>")
    if holder_name:
        parts.append(f"<BANKACCHOLDERNAME>{xml_escape(holder_name.strip())}</BANKACCHOLDERNAME>")
    if account_number:
        # ASLI TAG: <BANKDETAILS> (purana candidate BANKACCOUNTDETAILSACCOUNTNUMBER galat tha)
        parts.append(f"<BANKDETAILS>{xml_escape(account_number.strip())}</BANKDETAILS>")
    if ifs_code:
        # ASLI TAG: <IFSCODE> (purana BANKCONFIGIFSC export-only nikla)
        parts.append(f"<IFSCODE>{xml_escape(ifs_code.strip().upper())}</IFSCODE>")
    if swift_code:
        # ASLI TAG: <SWIFTCODE>
        parts.append(f"<SWIFTCODE>{xml_escape(swift_code.strip().upper())}</SWIFTCODE>")
    if branch:
        # ASLI TAG: <BRANCHNAME> (purana BANKBRANCHNAME export-only nikla)
        parts.append(f"<BRANCHNAME>{xml_escape(branch.strip())}</BRANCHNAME>")
    if bsr_code:
        # export-only; main form par field nahi hai (sub-screen ka hai)
        parts.append(f"<BANKBSRCODE>{xml_escape(bsr_code.strip())}</BANKBSRCODE>")
    if client_code:
        # export-only; UI save ke baad value gayab ho jati hai (sub-screen ka field)
        parts.append(f"<CLIENTCODE>{xml_escape(client_code.strip())}</CLIENTCODE>")

    if gst_applicable:
        # CORRECTED 2026-09-30 (UI-verified via Purchase A/c screenshot):
        # UI "GST applicability" dropdown = <GSTAPPLICABLE>&#4; ...</GSTAPPLICABLE>
        # (stock-item pattern). <ISGSTAPPLICABLE> is NOT the UI field --
        # Purchase A/c export had ISGSTAPPLICABLE=No but UI showed "Applicable".
        v = gst_applicable.strip().lower()
        word = "Applicable" if v in ("yes", "applicable") else "Not Applicable"
        parts.append(f"<GSTAPPLICABLE>&#4; {word}</GSTAPPLICABLE>")

    if tcs_applicable:
        # Tag VERIFIED ("No" on Purchase A/c export).
        v = tcs_applicable.strip().capitalize()
        parts.append(f"<ISTCSAPPLICABLE>{'Yes' if v == 'Yes' else 'No'}</ISTCSAPPLICABLE>")

    # ---- tax form: ALTER-ONLY (create_ledger Duties & Taxes ko refuse karta hai) ----
    if tax_type:
        # VERIFIED live export: <TAXTYPE>GST</TAXTYPE>.
        parts.append(f"<TAXTYPE>{xml_escape(tax_type.strip())}</TAXTYPE>")
    if duty_head:
        # VERIFIED live export: <GSTDUTYHEAD>CGST</GSTDUTYHEAD> (CGST/IGST par).
        parts.append(f"<GSTDUTYHEAD>{xml_escape(duty_head.strip())}</GSTDUTYHEAD>")
    if gst_type:
        # CAUTION: SGST anomaly -- SGST par <GSTTYPE>State Tax</GSTTYPE> tha jabki
        # CGST/IGST par <GSTTYPE>&#4; Not Applicable</GSTTYPE>. Live test + UI check pending.
        parts.append(f"<GSTTYPE>{xml_escape_keep_marker(gst_type.strip())}</GSTTYPE>")
    if tax_percentage is not None:
        # VERIFIED live export: <RATEOFTAXCALCULATION>2.50</RATEOFTAXCALCULATION>.
        parts.append(f"<RATEOFTAXCALCULATION>{float(tax_percentage)}</RATEOFTAXCALCULATION>")
    if rounding_method:
        # VERIFIED live export: <ROUNDINGMETHOD>&#4; Not Applicable</ROUNDINGMETHOD>.
        parts.append(f"<ROUNDINGMETHOD>{xml_escape_keep_marker(rounding_method.strip())}</ROUNDINGMETHOD>")
    if rounding_limit is not None:
        # VERIFIED live export: <ROUNDINGLIMIT>0</ROUNDINGLIMIT>.
        parts.append(f"<ROUNDINGLIMIT>{float(rounding_limit)}</ROUNDINGLIMIT>")
    if behave_as_duty:
        # VERIFIED live export: <ISBEHAVEASDUTY>No</ISBEHAVEASDUTY>.
        v = behave_as_duty.strip().capitalize()
        parts.append(f"<ISBEHAVEASDUTY>{'Yes' if v == 'Yes' else 'No'}</ISBEHAVEASDUTY>")

    parts.append("</LEDGER>")
    return "".join(parts)


# Nature -> native flags for TOP-LEVEL groups.
# From labs-infinitum/tallyprime-sdk (integration-tested against live TallyPrime):
# top-level groups use <PARENT/> (empty) + these flags. <PARENT>Primary</PARENT>
# is REJECTED by Tally ("Group 'Primary' does not exist!").
GROUP_NATURE_FLAGS = {
    "assets":      {"ISREVENUE": "No",  "AFFECTSGROSSPROFIT": "No",  "ISDEEMEDPOSITIVE": "Yes", "AFFECTSSTOCK": "No"},
    "liabilities": {"ISREVENUE": "No",  "AFFECTSGROSSPROFIT": "No",  "ISDEEMEDPOSITIVE": "No",  "AFFECTSSTOCK": "No"},
    "income":      {"ISREVENUE": "Yes", "AFFECTSGROSSPROFIT": "Yes", "ISDEEMEDPOSITIVE": "No",  "AFFECTSSTOCK": "No"},
    "expenses":    {"ISREVENUE": "Yes", "AFFECTSGROSSPROFIT": "No",  "ISDEEMEDPOSITIVE": "Yes", "AFFECTSSTOCK": "No"},
}


def build_group_message(name: str, action: str, parent: Optional[str] = None,
                        alias: Optional[str] = None, sub_ledger: Optional[str] = None,
                        affects_gross_profit: Optional[str] = None,
                        nature: Optional[str] = None,
                        used_for_calculation: Optional[str] = None,
                        allocate_method: Optional[str] = None,
                        is_addable: Optional[str] = None,
                        as_original: Optional[str] = None,
                        affects_stock: Optional[str] = None) -> str:
    """Build the <GROUP ...> TALLYMESSAGE body for Create / Alter / Delete.

    Two creation modes (mirrors Tally's UI form):
    - SUB-GROUP: parent = existing group name -> <PARENT>name</PARENT>.
      Nature is inherited; ISREVENUE/ISDEEMEDPOSITIVE are NOT sent.
    - TOP-LEVEL ("Primary" style): parent empty/omitted/"Primary" ->
      <PARENT/> (empty tag) + nature flags. "Primary" as a literal parent
      is rejected by Tally, so it is mapped to this pattern.

    Verified tags:
      alias -> LANGUAGENAME.LIST/NAME.LIST (LANGUAGEID 1033) [import-verified by us]
      sub_ledger -> <ISSUBLEDGER> [import-verified by us]
      affects_gross_profit -> <AFFECTSGROSSPROFIT> [import-verified by us]
      used_for_calculation -> <BASICGROUPISCALCULABLE> ("Used for Calculation")
        [import-verified by us 2026-09-30: Yes sticks]
      allocate_method -> <ADDLALLOCTYPE> ("Method to Allocate", purchase invoice)
        [NOT settable via XML on top-level groups as of 2026-09-30: Create and
        Alter both reported success but Tally kept "Not Applicable". The tool
        still sends it (native &#4; format) for sub-groups / future use.]
      is_addable -> <ISADDABLE>, as_original -> <ASORIGINAL>,
      affects_stock -> <AFFECTSSTOCK>
      (last five: real native tags, live-readable; UI-field mapping for
      ISADDABLE/ASORIGINAL vs "Nett Debit/Credit Balances" NOT yet verified -
      use preview + Tally UI check.)
    """
    n = xml_escape(name.strip())
    p = (parent or "").strip()
    top_level = (action == "Create" and (not p or p.lower() == "primary"))

    if top_level:
        key = (nature or "").strip().lower()
        flags = GROUP_NATURE_FLAGS.get(key)
        if flags is None:
            raise ValueError(
                "nature is required for top-level groups: "
                "Assets / Liabilities / Income / Expenses")
        parts = [f'<GROUP NAME="{n}" RESERVEDNAME="">', f"<NAME>{n}</NAME>", "<PARENT/>"]
        for tag, val in flags.items():
            if tag == "AFFECTSGROSSPROFIT" and affects_gross_profit:
                val = affects_gross_profit.strip()
            if tag == "AFFECTSSTOCK" and affects_stock:
                val = affects_stock.strip()
            parts.append(f"<{tag}>{xml_escape(val)}</{tag}>")
    else:
        parts = [f'<GROUP NAME="{n}" ACTION="{action}">', f"<NAME>{n}</NAME>"]
        if action != "Delete" and p:
            parts.append(f"<PARENT>{xml_escape(p)}</PARENT>")

    if action != "Delete":
        if alias:
            a = xml_escape(alias.strip())
            parts.append(
                f"<LANGUAGENAME.LIST><NAME.LIST><NAME>{n}</NAME><NAME>{a}</NAME>"
                f"</NAME.LIST><LANGUAGEID>1033</LANGUAGEID></LANGUAGENAME.LIST>"
            )
        if sub_ledger:
            parts.append(f"<ISSUBLEDGER>{xml_escape(sub_ledger.strip())}</ISSUBLEDGER>")
        if not top_level and affects_gross_profit:
            # top-level already handled above (nature default + override)
            parts.append(f"<AFFECTSGROSSPROFIT>{xml_escape(affects_gross_profit.strip())}</AFFECTSGROSSPROFIT>")
        if used_for_calculation:
            parts.append(f"<BASICGROUPISCALCULABLE>{xml_escape(used_for_calculation.strip())}</BASICGROUPISCALCULABLE>")
        if allocate_method:
            v = allocate_method.strip()
            if v.startswith("&#4;"):
                v = v[4:].strip()
            # Native export shows <ADDLALLOCTYPE>&#4; Not Applicable</ADDLALLOCTYPE>:
            # Tally stores dropdown values with the &#4; prefix (same lesson as
            # GSTAPPLICABLE). Accept the friendly value, emit native format.
            parts.append(f"<ADDLALLOCTYPE>&#4; {xml_escape(v)}</ADDLALLOCTYPE>")
        if is_addable:
            parts.append(f"<ISADDABLE>{xml_escape(is_addable.strip())}</ISADDABLE>")
        if as_original:
            parts.append(f"<ASORIGINAL>{xml_escape(as_original.strip())}</ASORIGINAL>")
    parts.append("</GROUP>")
    return "".join(parts)


def build_group_alter_message(name: str, new_name: Optional[str] = None,
                              parent: Optional[str] = None,
                              make_top_level: bool = False,
                              nature: Optional[str] = None,
                              alias: Optional[str] = None,
                              sub_ledger: Optional[str] = None,
                              affects_gross_profit: Optional[str] = None,
                              used_for_calculation: Optional[str] = None,
                              is_addable: Optional[str] = None,
                              as_original: Optional[str] = None,
                              affects_stock: Optional[str] = None) -> str:
    """Build the <GROUP ...> TALLYMESSAGE body for Alter.

    Only provided fields are emitted (omitted = left unchanged):
    - new_name: rename (NAME attribute keeps the OLD name so Tally finds it).
    - parent: move under this group (name), or None to leave unchanged.
    - make_top_level: True -> <PARENT/> (move to Primary; needs nature).
    - nature: re-classify a primary group (Assets/Liabilities/Income/Expenses);
      explicit affects_gross_profit / affects_stock override its defaults.

    NOTE: allocate_method is deliberately NOT here - live-tested 2026-09-30:
    Tally silently ignores ADDLALLOCTYPE via XML (import reports success but
    the value stays "Not Applicable"). Set it in the Tally UI.
    """
    n = xml_escape(name.strip())
    parts = [f'<GROUP NAME="{n}" ACTION="Alter">']
    if new_name:
        parts.append(f"<NAME>{xml_escape(new_name.strip())}</NAME>")
    if make_top_level:
        parts.append("<PARENT/>")
    elif parent is not None:
        parts.append(f"<PARENT>{xml_escape(parent.strip())}</PARENT>")
    if nature:
        key = (nature or "").strip().lower()
        flags = GROUP_NATURE_FLAGS.get(key)
        if flags is None:
            raise ValueError(
                "nature must be one of: Assets, Liabilities, Income, Expenses")
        for tag, val in flags.items():
            if tag == "AFFECTSGROSSPROFIT" and affects_gross_profit:
                val = affects_gross_profit.strip()
            if tag == "AFFECTSSTOCK" and affects_stock:
                val = affects_stock.strip()
            parts.append(f"<{tag}>{xml_escape(val)}</{tag}>")
    else:
        if affects_gross_profit:
            parts.append(
                f"<AFFECTSGROSSPROFIT>{xml_escape(affects_gross_profit.strip())}"
                f"</AFFECTSGROSSPROFIT>")
        if affects_stock:
            parts.append(
                f"<AFFECTSSTOCK>{xml_escape(affects_stock.strip())}</AFFECTSSTOCK>")
    if alias:
        a = xml_escape(alias.strip())
        nn = xml_escape((new_name or name).strip())
        parts.append(
            f"<LANGUAGENAME.LIST><NAME.LIST><NAME>{nn}</NAME><NAME>{a}</NAME>"
            f"</NAME.LIST><LANGUAGEID>1033</LANGUAGEID></LANGUAGENAME.LIST>"
        )
    if sub_ledger:
        parts.append(f"<ISSUBLEDGER>{xml_escape(sub_ledger.strip())}</ISSUBLEDGER>")
    if used_for_calculation:
        parts.append(
            f"<BASICGROUPISCALCULABLE>{xml_escape(used_for_calculation.strip())}"
            f"</BASICGROUPISCALCULABLE>")
    if is_addable:
        parts.append(f"<ISADDABLE>{xml_escape(is_addable.strip())}</ISADDABLE>")
    if as_original:
        parts.append(f"<ASORIGINAL>{xml_escape(as_original.strip())}</ASORIGINAL>")
    parts.append("</GROUP>")
    return "".join(parts)


def build_vouchertype_message(name: str, action: str, parent: Optional[str] = None,
                              new_name: Optional[str] = None) -> str:
    """Build the <VOUCHERTYPE ...> TALLYMESSAGE body for Create / Alter / Delete.

    NOTE: parent must be an EXISTING base voucher type (e.g. "Payment",
    "Receipt", "Journal", "Sales", "Purchase"). Verified 2026-09-28.
    """
    n = xml_escape(name.strip())
    display = xml_escape(new_name.strip()) if new_name else n
    parts = [f'<VOUCHERTYPE NAME="{n}" ACTION="{action}">', f"<NAME>{display}</NAME>"]
    if action not in ("Delete", "Alter") and parent:
        parts.append(f"<PARENT>{xml_escape(parent.strip())}</PARENT>")
    parts.append("</VOUCHERTYPE>")
    return "".join(parts)


def build_stockgroup_message(
    name: str,
    action: str,
    hsn_code: Optional[str] = None,
    taxability: Optional[str] = None,
    gst_rate: Optional[float] = None,
    applicable_from: Optional[str] = None,
    new_name: Optional[str] = None,
) -> str:
    """Build the <STOCKGROUP ...> TALLYMESSAGE body for Create / Alter / Delete.

    Verified 2026-09-28 against live Tally Prime:
    - Top-level stock groups take NO <PARENT> ("Primary" is rejected by Tally).
    - <GSTAPPLICABLE> is NOT a Stock Group field (only Stock Item / Ledger).
    - The HSN/GST "Specific" UI option is stored as
      <SRCOFHSNDETAILS>Specify Details Here</SRCOFHSNDETAILS> /
      <SRCOFGSTDETAILS>Specify Details Here</SRCOFGSTDETAILS>.
      Without these, Tally keeps "As per Company/Stock Group".
    - Taxability values: "Taxable", "Exempt", "Nil Rated", "Non-GST".
    - For "Taxable", gst_rate (total %) is split CGST=gst_rate/2,
      SGST=gst_rate/2, IGST=gst_rate under STATENAME "&#4; Any".
    - HSN/GST details are applied via Alter (Create with nested lists
      returned CREATED:0 in testing; Create basic + Alter works).
    """
    n = xml_escape(name.strip())
    display = xml_escape(new_name.strip()) if new_name else n
    parts = [f'<STOCKGROUP NAME="{n}" Action="{action}">', f"<NAME>{display}</NAME>"]
    if action != "Delete" and (hsn_code or taxability):
        af = yyyymmdd(applicable_from)
        if hsn_code:
            parts.append(
                "<HSNDETAILS.LIST>"
                f"<APPLICABLEFROM>{af}</APPLICABLEFROM>"
                "<SRCOFHSNDETAILS>Specify Details Here</SRCOFHSNDETAILS>"
                f"<HSNCODE>{xml_escape(hsn_code.strip())}</HSNCODE>"
                "</HSNDETAILS.LIST>"
            )
        if taxability:
            tax = taxability.strip()
            parts.append(
                "<GSTDETAILS.LIST>"
                f"<APPLICABLEFROM>{af}</APPLICABLEFROM>"
                "<SRCOFGSTDETAILS>Specify Details Here</SRCOFGSTDETAILS>"
                f"<TAXABILITY>{xml_escape(tax)}</TAXABILITY>"
                "<SUPPLYTYPE>Goods</SUPPLYTYPE>"
            )
            if tax.lower() == "taxable" and gst_rate:
                half = gst_rate / 2
                # Format without trailing zeros (2.5 stays 2.5, 6.0 -> 6)
                def _fmt(v: float) -> str:
                    return ("%g" % v)
                parts.append(
                    "<STATEWISEDETAILS.LIST>"
                    "<STATENAME>&#4; Any</STATENAME>"
                    "<RATEDETAILS.LIST>"
                    "<GSTRATEDUTYHEAD>CGST</GSTRATEDUTYHEAD>"
                    f"<GSTRATE>{_fmt(half)}</GSTRATE>"
                    "</RATEDETAILS.LIST>"
                    "<RATEDETAILS.LIST>"
                    "<GSTRATEDUTYHEAD>SGST/UTGST</GSTRATEDUTYHEAD>"
                    f"<GSTRATE>{_fmt(half)}</GSTRATE>"
                    "</RATEDETAILS.LIST>"
                    "<RATEDETAILS.LIST>"
                    "<GSTRATEDUTYHEAD>IGST</GSTRATEDUTYHEAD>"
                    f"<GSTRATE>{_fmt(gst_rate)}</GSTRATE>"
                    "</RATEDETAILS.LIST>"
                    "</STATEWISEDETAILS.LIST>"
                )
            parts.append("</GSTDETAILS.LIST>")
    parts.append("</STOCKGROUP>")
    return "".join(parts)


def build_stockcategory_message(name: str, action: str,
                                new_name: Optional[str] = None) -> str:
    """Build the <STOCKCATEGORY ...> TALLYMESSAGE body for Create / Alter / Delete.

    Verified 2026-09-28 against live Tally Prime:
    - Basic create: <STOCKCATEGORY Action="Create"><NAME>X</NAME></STOCKCATEGORY>
    - Alter: <STOCKCATEGORY NAME="old" ACTION="Alter"><NAME>new</NAME></STOCKCATEGORY>
    - Top-level categories take NO <PARENT>.
    - Delete: UPPERCASE ACTION="Delete" via Import Data.
    """
    n = xml_escape(name.strip())
    display = xml_escape(new_name.strip()) if new_name else n
    parts = [f'<STOCKCATEGORY NAME="{n}" ACTION="{action}">', f"<NAME>{display}</NAME>"]
    parts.append("</STOCKCATEGORY>")
    return "".join(parts)


def build_collection_export_envelope(
    collection_id: str,
    object_type: str,
    fields: list,
    company: Optional[str] = None,
) -> str:
    """Build a Collection Export envelope to list masters from Tally.

    Verified 2026-09-28 against live Tally Prime (deep research + live test):
    - Header MUST have <VERSION>1</VERSION> + <TYPE>Collection</TYPE> + <ID>.
    - <TALLYREQUEST> must be exactly "Export" (NOT "Export Data").
    - <TYPE>object_type</TYPE> is a CHILD tag inside COLLECTION (not attribute).
    - One <NATIVEMETHOD> per field to fetch.
    - Pass <SVCURRENTCOMPANY> to avoid silent empty results on wrong company.
    """
    nativemethods = "".join(f"<NATIVEMETHOD>{f}</NATIVEMETHOD>" for f in fields)
    company_tag = (
        f"<SVCURRENTCOMPANY>{xml_escape(company)}</SVCURRENTCOMPANY>"
        if company else ""
    )
    return (
        "<ENVELOPE>"
        "<HEADER>"
        "<VERSION>1</VERSION>"
        "<TALLYREQUEST>Export</TALLYREQUEST>"
        "<TYPE>Collection</TYPE>"
        f"<ID>{xml_escape(collection_id)}</ID>"
        "</HEADER>"
        "<BODY><DESC><STATICVARIABLES>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"{company_tag}"
        "</STATICVARIABLES><TDL><TDLMESSAGE>"
        f'<COLLECTION NAME="{xml_escape(collection_id)}" ISMODIFY="No">'
        f"<TYPE>{xml_escape(object_type)}</TYPE>"
        f"{nativemethods}"
        "</COLLECTION>"
        "</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
    )


def parse_collection_names(response_xml: str) -> list:
    """Extract unique <NAME> values from a collection export response.

    Uses a real XML parser: CDATA sections, entities and namespaces are
    handled; malformed responses raise instead of returning partial data.
    """
    root = _parse_xml_safe(response_xml)
    # dedupe preserving order
    seen = set()
    unique = []
    for el in _find_all_by_local(root, "NAME"):
        nn = "".join(el.itertext()).strip()
        if nn and nn not in seen:
            seen.add(nn)
            unique.append(nn)
    return unique


def parse_ledger_parents(response_xml: str) -> list:
    """Extract (name, parent-group) pairs from a Ledger collection export.

    Tally returns each ledger as <LEDGER NAME="..."> with a
    <PARENT TYPE="String">Group</PARENT> child. Verified 2026-09-30.
    """
    root = _parse_xml_safe(response_xml)
    pairs = []
    seen = set()
    for el in _find_all_by_local(root, "LEDGER"):
        name = (el.get("NAME") or "").strip()
        parent = ""
        for child in el:
            if _local(child.tag) == "PARENT":
                parent = "".join(child.itertext()).strip()
                break
        if name and name not in seen:
            seen.add(name)
            pairs.append({"name": name, "group": parent})
    return pairs


def build_stockitem_message(
    name: str,
    action: str,
    unit: Optional[str] = None,
    group: Optional[str] = None,
    category: Optional[str] = None,
) -> str:
    """Build the <STOCKITEM ...> TALLYMESSAGE body for Create / Alter / Delete.

    Verified 2026-09-28 against live Tally Prime:
    - Create: <STOCKITEM Action="Create"><NAME>X</NAME><BASEUNITS>Qtls</BASEUNITS></STOCKITEM>
    - Group assign (Under field): <PARENT>GroupName</PARENT> -- PLAIN name!
      NEVER use &#4; prefix in imports (export-only artifact; Tally silently
      ignores it but still returns ALTERED:1).
    - Workflow: create group FIRST, then item; Under defaults to Primary,
      assign group via PARENT when user asks.
    """
    n = xml_escape(name.strip())
    if action == "Delete":
        return f'<STOCKITEM NAME="{n}" ACTION="Delete"><NAME>{n}</NAME></STOCKITEM>'
    parts = [f'<STOCKITEM NAME="{n}" Action="{action}">', f"<NAME>{n}</NAME>"]
    if unit:
        parts.append(f"<BASEUNITS>{xml_escape(unit.strip())}</BASEUNITS>")
    if group:
        parts.append(f"<PARENT>{xml_escape(group.strip())}</PARENT>")
    if category:
        parts.append(f"<CATEGORY>{xml_escape(category.strip())}</CATEGORY>")
    parts.append("</STOCKITEM>")
    return "".join(parts)


def build_godown_message(name: str, action: str,
                         parent: Optional[str] = None,
                         new_name: Optional[str] = None) -> str:
    """Build the <GODOWN ...> TALLYMESSAGE body for Create / Alter / Delete.

    Verified 2026-09-29 against live Tally Prime:
    - Create: <GODOWN Action="Create"><NAME>X</NAME></GODOWN>
    - Alter: <GODOWN NAME="old" Action="Alter"><NAME>new</NAME></GODOWN>
    - Sub-godown: add <PARENT>ParentGodownName</PARENT> (plain exact name).
    - Prerequisite: "Maintain multiple godowns" ON in company (F11).
    """
    n = xml_escape(name.strip())
    if action == "Delete":
        return f'<GODOWN NAME="{n}" ACTION="Delete"><NAME>{n}</NAME></GODOWN>'
    display = xml_escape(new_name.strip()) if new_name else n
    parts = [f'<GODOWN NAME="{n}" Action="{action}">', f"<NAME>{display}</NAME>"]
    if parent:
        parts.append(f"<PARENT>{xml_escape(parent.strip())}</PARENT>")
    parts.append("</GODOWN>")
    return "".join(parts)


def build_unit_message(name: str, action: str,
                         formal_name: str = "") -> str:
    """Build the <UNIT ...> TALLYMESSAGE body for Create / Alter / Delete.

    Verified 2026-10-03 against live Tally Prime:
    - Create: <UNIT Action="Create"><NAME>kg</NAME><ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>
      <FORMALNAME>Kilogram</FORMALNAME></UNIT>
      (NAME is a CHILD tag, not an attribute; ISSIMPLEUNIT is REQUIRED.
      <UNIT NAME="kg" ACTION="Create"> gives "BAD UNIT NAME".)
    - Alter: <UNIT NAME="kg" ACTION="Alter"> + fields to change.
    - Delete: <UNIT NAME="kg" ACTION="Delete"><NAME>kg</NAME></UNIT>
    """
    n = xml_escape(name.strip())
    if action == "Delete":
        return f'<UNIT NAME="{n}" ACTION="Delete"><NAME>{n}</NAME></UNIT>'
    parts = [f'<UNIT NAME="{n}" Action="{action}">', f"<NAME>{n}</NAME>",
             "<ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>"]
    if formal_name and formal_name.strip():
        parts.append(f"<FORMALNAME>{xml_escape(formal_name.strip())}</FORMALNAME>")
    parts.append("</UNIT>")
    return "".join(parts)


def build_purchase_voucher_message(
    date: str,
    party_name: str,
    items: list,
    charges: Optional[list] = None,
    reference: str = "",
    narration: str = "",
    party_address_lines: Optional[list] = None,
    party_state: str = "",
    party_country: str = "",
    party_pincode: str = "",
    party_gstin: str = "",
    voucher_number: str = "",
    remote_id: str = "",
) -> str:
    """Build the <VOUCHER ...> TALLYMESSAGE body for a Purchase item-invoice.

    Verified 2026-09-29 against live Tally Prime :
    - Item invoice mode: VCHENTRYMODE=Item Invoice, ISINVOICE=Yes,
      OBJVIEW=Invoice Voucher View.
    - DATE + EFFECTIVEDATE both sent (EFFECTIVEDATE=DATE); missing
      EFFECTIVEDATE triggers Tally's "Voucher date is missing" error
      (fixed 2026-10-06 after the Shri Hari Industries bill live-test).
    - Inventory lines go in ALLINVENTORYENTRIES.LIST (negative amounts,
      ISDEEMEDPOSITIVE=Yes) with BATCHALLOCATIONS + ACCOUNTINGALLOCATIONS.
    - Extra charges (Insurance/Hammali/Freight) go as voucher-level
      LEDGERENTRIES.LIST with ONLY 3 tags: LEDGERNAME + ISDEEMEDPOSITIVE=Yes
      + negative AMOUNT. NO ISPARTYLEDGER / ISLASTDEEMEDPOSITIVE on charges!
    - Supplier block comes FIRST with ISPARTYLEDGER=Yes (Day Book shows the
      FIRST LEDGERENTRIES.LIST carrying ISPARTYLEDGER=Yes).
    - Party Details do NOT auto-fill on XML import -- inject explicitly:
      PARTYMAILINGNAME, ADDRESS.LIST per line, STATENAME, COUNTRYOFRESIDENCE,
      PINCODE, PARTYGSTIN, PLACEOFSUPPLY (all direct children of VOUCHER).
    - Amounts must net to zero: supplier(+) = items(-) + charges(-).

    Args:
        date: YYYYMMDD.
        party_name: Supplier ledger name (exact).
        items: list of dicts: {name, qty, unit, rate, amount, godown}.
        charges: list of dicts: {ledger, amount} (positive numbers;
            stored as negative debits). e.g. [{"ledger": "Insurance",
            "amount": 415}].
        reference: Supplier bill/invoice number.
        narration: Voucher narration.
        party_address_lines: e.g. ["123, Industrial Area",
            "New Delhi - 110001"].
        party_state / party_country / party_pincode / party_gstin:
            Party Details fields.
        voucher_number: Voucher number hint (Tally may auto-number).
    """
    p = xml_escape(party_name.strip())
    d = yyyymmdd(date)
    # REMOTEID: stamp explicit ID for deletability + idempotency (verified 2026-09-29:
    # vouchers without explicit REMOTEID get Tally-internal IDs that can't be deleted via XML)
    rid = remote_id.strip() if remote_id else ""
    if not rid and reference:
        rid = f"{_PREFIX}-PUR-{d}-{reference.strip()}"
    vch_open = '<VOUCHER VCHTYPE="Purchase" ACTION="Create" OBJVIEW="Invoice Voucher View"'
    if rid:
        vch_open += f' REMOTEID="{xml_escape(rid)}"'
    vch_open += ">"
    parts = [
        vch_open,
        f"<DATE>{d}</DATE>",
        f"<EFFECTIVEDATE>{d}</EFFECTIVEDATE>",
        "<VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>",
        "<VCHENTRYMODE>Item Invoice</VCHENTRYMODE>",
        "<ISINVOICE>Yes</ISINVOICE>",
    ]
    if voucher_number:
        parts.append(f"<VOUCHERNUMBER>{xml_escape(voucher_number)}</VOUCHERNUMBER>")
    if reference:
        parts.append(f"<REFERENCE>{xml_escape(reference)}</REFERENCE>")
    if narration:
        parts.append(f"<NARRATION>{xml_escape(narration)}</NARRATION>")

    # -- party identity + explicit Party Details (no auto-fill on import) --
    parts.append(f"<PARTYNAME>{p}</PARTYNAME>")
    parts.append(f"<PARTYLEDGERNAME>{p}</PARTYLEDGERNAME>")
    parts.append(f"<PARTYMAILINGNAME>{p}</PARTYMAILINGNAME>")
    parts.append(f"<CONSIGNEEMAILINGNAME>{p}</CONSIGNEEMAILINGNAME>")
    parts.append(f"<BASICBASEPARTYNAME>{p}</BASICBASEPARTYNAME>")
    parts.append(f"<BASICBUYERNAME>{p}</BASICBUYERNAME>")
    for line in (party_address_lines or []):
        line = line.strip()
        if line:
            parts.append(
                f"<ADDRESS.LIST><ADDRESS>{xml_escape(line)}</ADDRESS></ADDRESS.LIST>"
            )
    if party_state:
        s = xml_escape(party_state.strip())
        parts.append(f"<STATENAME>{s}</STATENAME>")
        parts.append(f"<CONSIGNEESTATENAME>{s}</CONSIGNEESTATENAME>")
        parts.append(f"<PLACEOFSUPPLY>{s}</PLACEOFSUPPLY>")
    if party_country:
        c = xml_escape(party_country.strip())
        parts.append(f"<COUNTRYNAME>{c}</COUNTRYNAME>")
        parts.append(f"<COUNTRYOFRESIDENCE>{c}</COUNTRYOFRESIDENCE>")
    if party_pincode:
        parts.append(f"<PINCODE>{xml_escape(party_pincode.strip())}</PINCODE>")
    if party_gstin:
        parts.append(f"<PARTYGSTIN>{xml_escape(party_gstin.strip())}</PARTYGSTIN>")
        parts.append("<GSTREGISTRATIONTYPE>Regular</GSTREGISTRATIONTYPE>")

    # -- totals --
    items_total = sum(float(it["amount"]) for it in items)
    charges_total = sum(float(ch["amount"]) for ch in (charges or []))
    supplier_total = items_total + charges_total

    # -- supplier block FIRST (unique ISPARTYLEDGER=Yes) --
    parts.append("<LEDGERENTRIES.LIST>")
    parts.append(f"<LEDGERNAME>{p}</LEDGERNAME>")
    parts.append("<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>")
    parts.append("<ISPARTYLEDGER>Yes</ISPARTYLEDGER>")
    parts.append("<ISLASTDEEMEDPOSITIVE>No</ISLASTDEEMEDPOSITIVE>")
    parts.append(f"<AMOUNT>{supplier_total:.2f}</AMOUNT>")
    parts.append("</LEDGERENTRIES.LIST>")

    # -- charge blocks (minimal 3-tag) --
    for ch in (charges or []):
        ledger = xml_escape(str(ch["ledger"]).strip())
        amt = float(ch["amount"])
        parts.append("<LEDGERENTRIES.LIST>")
        parts.append(f"<LEDGERNAME>{ledger}</LEDGERNAME>")
        parts.append("<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>")
        parts.append(f"<AMOUNT>{-amt:.2f}</AMOUNT>")
        parts.append("</LEDGERENTRIES.LIST>")

    # -- inventory lines --
    for it in items:
        iname = xml_escape(str(it["name"]).strip())
        qty = float(it["qty"])
        unit = xml_escape(str(it.get("unit", "")).strip())
        rate = float(it["rate"])
        amt = float(it["amount"])
        godown = xml_escape(str(it.get("godown", "Main Location")).strip())
        qty_str = f"{qty:g} {unit}".strip()
        parts.append("<ALLINVENTORYENTRIES.LIST>")
        parts.append(f"<STOCKITEMNAME>{iname}</STOCKITEMNAME>")
        parts.append("<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>")
        parts.append(f"<RATE>{rate:g}/{unit}</RATE>")
        parts.append(f"<AMOUNT>{-amt:.2f}</AMOUNT>")
        parts.append(f"<ACTUALQTY>{qty_str}</ACTUALQTY>")
        parts.append(f"<BILLEDQTY>{qty_str}</BILLEDQTY>")
        parts.append("<BATCHALLOCATIONS.LIST>")
        parts.append(f"<GODOWNNAME>{godown}</GODOWNNAME>")
        parts.append("<BATCHNAME>Primary Batch</BATCHNAME>")
        parts.append(f"<AMOUNT>{-amt:.2f}</AMOUNT>")
        parts.append(f"<ACTUALQTY>{qty_str}</ACTUALQTY>")
        parts.append(f"<BILLEDQTY>{qty_str}</BILLEDQTY>")
        parts.append("</BATCHALLOCATIONS.LIST>")
        parts.append("<ACCOUNTINGALLOCATIONS.LIST>")
        parts.append("<LEDGERNAME>Purchase A/c</LEDGERNAME>")
        parts.append("<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>")
        parts.append(f"<AMOUNT>{-amt:.2f}</AMOUNT>")
        parts.append("</ACCOUNTINGALLOCATIONS.LIST>")
        parts.append("</ALLINVENTORYENTRIES.LIST>")

    parts.append("</VOUCHER>")
    return "".join(parts)


def build_debit_note_message(
    date: str,
    party_name: str,
    items: list,
    charges: list = None,
    reference: str = "",
    narration: str = "",
    voucher_number: str = "",
    remote_id: str = "",
) -> str:
    """Build the <VOUCHER ...> TALLYMESSAGE body for a Debit Note (purchase return).

    Debit Note = Purchase ka mirror. Supplier ko debit (maal wapas).
    Verified 2026-10-04 via deep research .

    - VCHTYPE=Debit Note, OBJVIEW=Invoice Voucher View,
      VCHENTRYMODE=Item Invoice, ISINVOICE=Yes.
    - Supplier block FIRST: ISDEEMEDPOSITIVE=Yes + NEGATIVE amount
      (debit supplier). ISPARTYLEDGER=Yes.
    - Charges (CGST/SGST reversal): LEDGERNAME + ISDEEMEDPOSITIVE=No +
      POSITIVE amount (credit).
    - Inventory: stock OUT -- ALLINVENTORYENTRIES.LIST with
      ISDEEMEDPOSITIVE=No, negative AMOUNT, positive ACTUALQTY.
    - Net rule: supplier(-) + items(+) + charges(+) = 0.

    Args:
        date: YYYYMMDD.
        party_name: Supplier ledger name (exact).
        items: list of {name, qty, unit, rate, amount, godown}.
        charges: list of {ledger, amount} (e.g. CGST/SGST reversal).
        reference: Debit note reference number.
        narration: Voucher narration.
        voucher_number: Voucher number hint.
        remote_id: Explicit REMOTEID (auto: BJB-DN-<date>-<reference>).
    """
    p = xml_escape(party_name.strip())
    d = yyyymmdd(date)
    rid = remote_id.strip() if remote_id else ""
    if not rid and reference:
        rid = f"{_PREFIX}-DN-{d}-{reference.strip()}"
    vch_open = '<VOUCHER VCHTYPE="Debit Note" ACTION="Create" OBJVIEW="Invoice Voucher View"'
    if rid:
        vch_open += f' REMOTEID="{xml_escape(rid)}"'
    vch_open += ">"
    parts = [
        vch_open,
        f"<DATE>{d}</DATE>",
        f"<EFFECTIVEDATE>{d}</EFFECTIVEDATE>",
        "<VOUCHERTYPENAME>Debit Note</VOUCHERTYPENAME>",
        "<VCHENTRYMODE>Item Invoice</VCHENTRYMODE>",
        "<ISINVOICE>Yes</ISINVOICE>",
    ]
    if voucher_number.strip():
        parts.append(f"<VOUCHERNUMBER>{xml_escape(voucher_number.strip())}</VOUCHERNUMBER>")
    if reference.strip():
        parts.append(f"<REFERENCE>{xml_escape(reference.strip())}</REFERENCE>")
    if narration.strip():
        parts.append(f"<NARRATION>{xml_escape(narration.strip())}</NARRATION>")
    parts.append(f"<PARTYNAME>{p}</PARTYNAME>")
    parts.append(f"<PARTYLEDGERNAME>{p}</PARTYLEDGERNAME>")

    items_total = sum(float(it["amount"]) for it in items)
    charges_total = sum(float(ch["amount"]) for ch in (charges or []))
    supplier_total = items_total + charges_total

    # Supplier debit (negative)
    parts.append("<LEDGERENTRIES.LIST>")
    parts.append(f"<LEDGERNAME>{p}</LEDGERNAME>")
    parts.append("<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>")
    parts.append("<ISPARTYLEDGER>Yes</ISPARTYLEDGER>")
    parts.append("<ISLASTDEEMEDPOSITIVE>Yes</ISLASTDEEMEDPOSITIVE>")
    parts.append(f"<AMOUNT>{-supplier_total:.2f}</AMOUNT>")
    parts.append("</LEDGERENTRIES.LIST>")

    # Charges credit (positive) -- GST reversal
    for ch in (charges or []):
        ledger = xml_escape(str(ch["ledger"]).strip())
        amt = float(ch["amount"])
        parts.append("<LEDGERENTRIES.LIST>")
        parts.append(f"<LEDGERNAME>{ledger}</LEDGERNAME>")
        parts.append("<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>")
        parts.append(f"<AMOUNT>{amt:.2f}</AMOUNT>")
        parts.append("</LEDGERENTRIES.LIST>")

    # Inventory: stock OUT
    for it in items:
        iname = xml_escape(str(it["name"]).strip())
        qty = float(it["qty"])
        unit = xml_escape(str(it.get("unit", "")).strip())
        rate = float(it["rate"])
        amt = float(it["amount"])
        godown = xml_escape(str(it.get("godown", "Main Location")).strip())
        qty_str = f"{qty:g} {unit}".strip()
        parts.append("<ALLINVENTORYENTRIES.LIST>")
        parts.append(f"<STOCKITEMNAME>{iname}</STOCKITEMNAME>")
        parts.append("<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>")
        parts.append(f"<RATE>{rate:g}/{unit}</RATE>")
        parts.append(f"<AMOUNT>{-amt:.2f}</AMOUNT>")
        parts.append(f"<ACTUALQTY>{qty_str}</ACTUALQTY>")
        parts.append(f"<BILLEDQTY>{qty_str}</BILLEDQTY>")
        parts.append("<BATCHALLOCATIONS.LIST>")
        parts.append(f"<GODOWNNAME>{godown}</GODOWNNAME>")
        parts.append("<BATCHNAME>Primary Batch</BATCHNAME>")
        parts.append(f"<AMOUNT>{-amt:.2f}</AMOUNT>")
        parts.append(f"<ACTUALQTY>{qty_str}</ACTUALQTY>")
        parts.append(f"<BILLEDQTY>{qty_str}</BILLEDQTY>")
        parts.append("</BATCHALLOCATIONS.LIST>")
        parts.append("<ACCOUNTINGALLOCATIONS.LIST>")
        parts.append("<LEDGERNAME>Purchase A/c</LEDGERNAME>")
        parts.append("<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>")
        parts.append(f"<AMOUNT>{amt:.2f}</AMOUNT>")
        parts.append("</ACCOUNTINGALLOCATIONS.LIST>")
        parts.append("</ALLINVENTORYENTRIES.LIST>")

    parts.append("</VOUCHER>")
    return "".join(parts)


def build_credit_note_message(
    date: str,
    party_name: str,
    items: list,
    charges: list = None,
    reference: str = "",
    narration: str = "",
    voucher_number: str = "",
    remote_id: str = "",
) -> str:
    """Build the <VOUCHER ...> TALLYMESSAGE body for a Credit Note (sales return).

    Credit Note = Sales ka mirror. Customer ko credit (maal wapas aaya).
    Verified 2026-10-04 via deep research .

    - VCHTYPE=Credit Note, OBJVIEW=Invoice Voucher View,
      VCHENTRYMODE=Item Invoice, ISINVOICE=Yes.
    - Customer block FIRST: ISDEEMEDPOSITIVE=No + POSITIVE amount
      (credit customer). ISPARTYLEDGER=Yes.
    - Charges (CGST/SGST reversal): LEDGERNAME + ISDEEMEDPOSITIVE=Yes +
      NEGATIVE amount (debit).
    - Inventory: stock IN -- ALLINVENTORYENTRIES.LIST with
      ISDEEMEDPOSITIVE=Yes, positive AMOUNT, positive ACTUALQTY.
    - Net rule: customer(+) + items(-) + charges(-) = 0.

    Args:
        date: YYYYMMDD.
        party_name: Customer ledger name (exact).
        items: list of {name, qty, unit, rate, amount, godown}.
        charges: list of {ledger, amount} (e.g. CGST/SGST reversal).
        reference: Credit note reference number.
        narration: Voucher narration.
        voucher_number: Voucher number hint.
        remote_id: Explicit REMOTEID (auto: BJB-CN-<date>-<reference>).
    """
    p = xml_escape(party_name.strip())
    d = yyyymmdd(date)
    rid = remote_id.strip() if remote_id else ""
    if not rid and reference:
        rid = f"{_PREFIX}-CN-{d}-{reference.strip()}"
    vch_open = '<VOUCHER VCHTYPE="Credit Note" ACTION="Create" OBJVIEW="Invoice Voucher View"'
    if rid:
        vch_open += f' REMOTEID="{xml_escape(rid)}"'
    vch_open += ">"
    parts = [
        vch_open,
        f"<DATE>{d}</DATE>",
        f"<EFFECTIVEDATE>{d}</EFFECTIVEDATE>",
        "<VOUCHERTYPENAME>Credit Note</VOUCHERTYPENAME>",
        "<VCHENTRYMODE>Item Invoice</VCHENTRYMODE>",
        "<ISINVOICE>Yes</ISINVOICE>",
    ]
    if voucher_number.strip():
        parts.append(f"<VOUCHERNUMBER>{xml_escape(voucher_number.strip())}</VOUCHERNUMBER>")
    if reference.strip():
        parts.append(f"<REFERENCE>{xml_escape(reference.strip())}</REFERENCE>")
    if narration.strip():
        parts.append(f"<NARRATION>{xml_escape(narration.strip())}</NARRATION>")
    parts.append(f"<PARTYNAME>{p}</PARTYNAME>")
    parts.append(f"<PARTYLEDGERNAME>{p}</PARTYLEDGERNAME>")

    items_total = sum(float(it["amount"]) for it in items)
    charges_total = sum(float(ch["amount"]) for ch in (charges or []))
    customer_total = items_total + charges_total

    # Customer credit (positive)
    parts.append("<LEDGERENTRIES.LIST>")
    parts.append(f"<LEDGERNAME>{p}</LEDGERNAME>")
    parts.append("<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>")
    parts.append("<ISPARTYLEDGER>Yes</ISPARTYLEDGER>")
    parts.append("<ISLASTDEEMEDPOSITIVE>No</ISLASTDEEMEDPOSITIVE>")
    parts.append(f"<AMOUNT>{customer_total:.2f}</AMOUNT>")
    parts.append("</LEDGERENTRIES.LIST>")

    # Charges debit (negative) -- GST reversal
    for ch in (charges or []):
        ledger = xml_escape(str(ch["ledger"]).strip())
        amt = float(ch["amount"])
        parts.append("<LEDGERENTRIES.LIST>")
        parts.append(f"<LEDGERNAME>{ledger}</LEDGERNAME>")
        parts.append("<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>")
        parts.append(f"<AMOUNT>{-amt:.2f}</AMOUNT>")
        parts.append("</LEDGERENTRIES.LIST>")

    # Inventory: stock IN
    for it in items:
        iname = xml_escape(str(it["name"]).strip())
        qty = float(it["qty"])
        unit = xml_escape(str(it.get("unit", "")).strip())
        rate = float(it["rate"])
        amt = float(it["amount"])
        godown = xml_escape(str(it.get("godown", "Main Location")).strip())
        qty_str = f"{qty:g} {unit}".strip()
        parts.append("<ALLINVENTORYENTRIES.LIST>")
        parts.append(f"<STOCKITEMNAME>{iname}</STOCKITEMNAME>")
        parts.append("<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>")
        parts.append(f"<RATE>{rate:g}/{unit}</RATE>")
        parts.append(f"<AMOUNT>{amt:.2f}</AMOUNT>")
        parts.append(f"<ACTUALQTY>{qty_str}</ACTUALQTY>")
        parts.append(f"<BILLEDQTY>{qty_str}</BILLEDQTY>")
        parts.append("<BATCHALLOCATIONS.LIST>")
        parts.append(f"<GODOWNNAME>{godown}</GODOWNNAME>")
        parts.append("<BATCHNAME>Primary Batch</BATCHNAME>")
        parts.append(f"<AMOUNT>{amt:.2f}</AMOUNT>")
        parts.append(f"<ACTUALQTY>{qty_str}</ACTUALQTY>")
        parts.append(f"<BILLEDQTY>{qty_str}</BILLEDQTY>")
        parts.append("</BATCHALLOCATIONS.LIST>")
        parts.append("<ACCOUNTINGALLOCATIONS.LIST>")
        parts.append("<LEDGERNAME>Sales A/c</LEDGERNAME>")
        parts.append("<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>")
        parts.append(f"<AMOUNT>{-amt:.2f}</AMOUNT>")
        parts.append("</ACCOUNTINGALLOCATIONS.LIST>")
        parts.append("</ALLINVENTORYENTRIES.LIST>")

    parts.append("</VOUCHER>")
    return "".join(parts)


def build_sales_voucher_message(
        date: str,
        party_name: str,
        sale_type: str = "credit",
        items: list = None,
        charges: list = None,
        reference: str = "",
        narration: str = "",
        party_address_lines: list = None,
        party_state: str = "",
        party_country: str = "",
        party_pincode: str = "",
        party_gstin: str = "",
        voucher_number: str = "",
        remote_id: str = "",
        sales_ledger: str = "Sales A/c",
        buyer_name: str = "",
) -> str:
    """Build the <VOUCHER ...> TALLYMESSAGE body for a Sales item-invoice.

    Research: SALES_VOUCHER_RESEARCH.md (2026-09-29) -- official TallyHelp
    sample + 7 sources cross-verified. Sales is the EXACT MIRROR of purchase:
    every sign flips.
    - DATE + EFFECTIVEDATE both sent (EFFECTIVEDATE=DATE); missing
      EFFECTIVEDATE triggers Tally's "Voucher date is missing" error
      (fixed 2026-10-06 alongside the purchase builder).
    - Customer block FIRST: ISDEEMEDPOSITIVE=Yes, ISPARTYLEDGER=Yes,
      ISLASTDEEMEDPOSITIVE=Yes (mirrors ISDEEMEDPOSITIVE), AMOUNT negative.
    - Credit sales: BILLALLOCATIONS.LIST (NAME=reference, BILLTYPE=New Ref)
      keeps bill-wise outstandings correct. Cash sales: no bill allocations.
    - Charges/tax: minimal 3-tag (LEDGERNAME + ISDEEMEDPOSITIVE=No +
      positive AMOUNT). NO ISPARTYLEDGER on charges.
    - Items: ALLINVENTORYENTRIES.LIST with POSITIVE amounts,
      ISDEEMEDPOSITIVE=No; BATCHALLOCATIONS includes DESTINATIONGODOWNNAME
      (stock goes OUT); ACCOUNTINGALLOCATIONS -> sales ledger.
    - Net rule: customer(-) + items(+) + charges(+) = 0.
    - Party Details do NOT auto-fill on import -- injected explicitly.
    """
    items = items or []
    if sale_type not in ("credit", "cash"):
        raise ValueError("sale_type must be 'credit' or 'cash'")
    p = xml_escape(party_name.strip())
    d = yyyymmdd(date)
    rid = remote_id.strip() if remote_id else ""
    if not rid and reference:
        rid = f"{_PREFIX}-SAL-{d}-{reference.strip()}"
    vch_open = '<VOUCHER VCHTYPE="Sales" ACTION="Create" OBJVIEW="Invoice Voucher View"'
    if rid:
        vch_open += f' REMOTEID="{xml_escape(rid)}"'
    vch_open += ">"
    parts = [
        vch_open,
        f"<DATE>{d}</DATE>",
        f"<EFFECTIVEDATE>{d}</EFFECTIVEDATE>",
        "<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>",
        "<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>",
        "<VCHENTRYMODE>Item Invoice</VCHENTRYMODE>",
        "<ISINVOICE>Yes</ISINVOICE>",
    ]
    if voucher_number:
        parts.append(f"<VOUCHERNUMBER>{xml_escape(voucher_number)}</VOUCHERNUMBER>")
    if reference:
        parts.append(f"<REFERENCE>{xml_escape(reference)}</REFERENCE>")
    if narration:
        parts.append(f"<NARRATION>{xml_escape(narration)}</NARRATION>")

    # -- party identity + explicit Party Details (no auto-fill on import) --
    # buyer_name: cash sale me bill pe print hone wala naam (ledger wahi rahega)
    _display = xml_escape(buyer_name.strip()) if buyer_name and buyer_name.strip() else p
    parts.append(f"<PARTYNAME>{_display}</PARTYNAME>")
    parts.append(f"<PARTYLEDGERNAME>{p}</PARTYLEDGERNAME>")
    parts.append(f"<PARTYMAILINGNAME>{_display}</PARTYMAILINGNAME>")
    parts.append(f"<CONSIGNEEMAILINGNAME>{_display}</CONSIGNEEMAILINGNAME>")
    parts.append(f"<BASICBASEPARTYNAME>{_display}</BASICBASEPARTYNAME>")
    parts.append(f"<BASICBUYERNAME>{_display}</BASICBUYERNAME>")
    for line in (party_address_lines or []):
        line = line.strip()
        if line:
            parts.append(
                f"<ADDRESS.LIST><ADDRESS>{xml_escape(line)}</ADDRESS></ADDRESS.LIST>"
            )
    if party_state:
        s = xml_escape(party_state.strip())
        parts.append(f"<STATENAME>{s}</STATENAME>")
        parts.append(f"<CONSIGNEESTATENAME>{s}</CONSIGNEESTATENAME>")
        parts.append(f"<PLACEOFSUPPLY>{s}</PLACEOFSUPPLY>")
    if party_country:
        c = xml_escape(party_country.strip())
        parts.append(f"<COUNTRYNAME>{c}</COUNTRYNAME>")
        parts.append(f"<COUNTRYOFRESIDENCE>{c}</COUNTRYOFRESIDENCE>")
    if party_pincode:
        parts.append(f"<PINCODE>{xml_escape(party_pincode.strip())}</PINCODE>")
    if party_gstin:
        parts.append(f"<PARTYGSTIN>{xml_escape(party_gstin.strip())}</PARTYGSTIN>")
        parts.append("<GSTREGISTRATIONTYPE>Regular</GSTREGISTRATIONTYPE>")

    # -- totals (mirror of purchase) --
    items_total = sum(float(it["amount"]) for it in items)
    charges_total = sum(float(ch["amount"]) for ch in (charges or []))
    customer_total = items_total + charges_total

    # -- customer block FIRST (unique ISPARTYLEDGER=Yes), NEGATIVE amount --
    parts.append("<LEDGERENTRIES.LIST>")
    parts.append(f"<LEDGERNAME>{p}</LEDGERNAME>")
    parts.append("<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>")
    parts.append("<ISPARTYLEDGER>Yes</ISPARTYLEDGER>")
    parts.append("<ISLASTDEEMEDPOSITIVE>Yes</ISLASTDEEMEDPOSITIVE>")
    parts.append(f"<AMOUNT>{-customer_total:.2f}</AMOUNT>")
    if sale_type == "credit":
        bill_name = xml_escape(reference.strip()) if reference else "New Ref"
        parts.append("<BILLALLOCATIONS.LIST>")
        parts.append(f"<NAME>{bill_name}</NAME>")
        parts.append("<BILLTYPE>New Ref</BILLTYPE>")
        parts.append(f"<AMOUNT>{-customer_total:.2f}</AMOUNT>")
        parts.append("</BILLALLOCATIONS.LIST>")
    parts.append("</LEDGERENTRIES.LIST>")

    # -- charge/tax blocks (minimal 3-tag, POSITIVE) --
    for ch in (charges or []):
        ledger = xml_escape(str(ch["ledger"]).strip())
        amt = float(ch["amount"])
        parts.append("<LEDGERENTRIES.LIST>")
        parts.append(f"<LEDGERNAME>{ledger}</LEDGERNAME>")
        parts.append("<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>")
        parts.append(f"<AMOUNT>{amt:.2f}</AMOUNT>")
        parts.append("</LEDGERENTRIES.LIST>")

    # -- inventory lines (POSITIVE amounts, stock goes OUT) --
    sledger = xml_escape(sales_ledger.strip())
    for it in items:
        iname = xml_escape(str(it["name"]).strip())
        qty = float(it["qty"])
        unit = xml_escape(str(it.get("unit", "")).strip())
        rate = float(it["rate"])
        amt = float(it["amount"])
        godown = xml_escape(str(it.get("godown", "Main Location")).strip())
        qty_str = f"{qty:g} {unit}".strip()
        parts.append("<ALLINVENTORYENTRIES.LIST>")
        parts.append(f"<STOCKITEMNAME>{iname}</STOCKITEMNAME>")
        parts.append("<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>")
        parts.append(f"<RATE>{rate:g}/{unit}</RATE>")
        parts.append(f"<AMOUNT>{amt:.2f}</AMOUNT>")
        parts.append(f"<ACTUALQTY>{qty_str}</ACTUALQTY>")
        parts.append(f"<BILLEDQTY>{qty_str}</BILLEDQTY>")
        parts.append("<BATCHALLOCATIONS.LIST>")
        parts.append(f"<GODOWNNAME>{godown}</GODOWNNAME>")
        parts.append(f"<DESTINATIONGODOWNNAME>{godown}</DESTINATIONGODOWNNAME>")
        parts.append("<BATCHNAME>Primary Batch</BATCHNAME>")
        parts.append(f"<AMOUNT>{amt:.2f}</AMOUNT>")
        parts.append(f"<ACTUALQTY>{qty_str}</ACTUALQTY>")
        parts.append(f"<BILLEDQTY>{qty_str}</BILLEDQTY>")
        parts.append("</BATCHALLOCATIONS.LIST>")
        parts.append("<ACCOUNTINGALLOCATIONS.LIST>")
        parts.append(f"<LEDGERNAME>{sledger}</LEDGERNAME>")
        parts.append("<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>")
        parts.append(f"<AMOUNT>{amt:.2f}</AMOUNT>")
        parts.append("</ACCOUNTINGALLOCATIONS.LIST>")
        parts.append("</ALLINVENTORYENTRIES.LIST>")

    parts.append("</VOUCHER>")
    return "".join(parts)


def build_payment_voucher_message(
    date: str,
    debit_entries: list,
    credit_ledger: str = "Cash",
    reference: str = "",
    narration: str = "",
    voucher_number: str = "",
    remote_id: str = "",
    bank_transaction_type: str = "Others",
) -> str:
    """Build the <VOUCHER ...> TALLYMESSAGE body for a Payment voucher.

    Verified 2026-10-03 against live Tally Prime :
    - Accounting voucher: VCHTYPE=Payment, OBJVIEW=Accounting Voucher View,
      PERSISTEDVIEW=Accounting Voucher View. NO inventory lines.
    - Debit entries (party/expense): LEDGERNAME + ISDEEMEDPOSITIVE=Yes +
      positive AMOUNT. Party payments get ISPARTYLEDGER=Yes +
      ISLASTDEEMEDPOSITIVE=Yes and BILLALLOCATIONS.LIST (NAME=bill ref,
      BILLTYPE=Agst Ref) for bill-wise settlement.
    - Credit entry (Cash/Bank): LEDGERNAME + ISDEEMEDPOSITIVE=No +
      negative AMOUNT. NO ISPARTYLEDGER.
    - Net rule: debits(+) + credit(-) = 0.
    - EFFECTIVEDATE = DATE (both required for correct date display).

    Args:
        date: YYYYMMDD.
        debit_entries: list of dicts: {ledger, amount, bill_ref (optional),
            is_party (bool, default True for bill_ref entries)}.
            e.g. [{"ledger": "ABC Suppliers Pvt Ltd",
                   "amount": 5000, "bill_ref": "BS/25-26/1128"}]
        credit_ledger: Cash/Bank ledger name (default "Cash").
        reference: Payment reference/cheque number.
        narration: Voucher narration.
        voucher_number: Voucher number hint (Tally may auto-number).
        remote_id: Explicit REMOTEID (auto-generated as BJB-PAY-<date>-<ref>
            if reference given and remote_id empty).
    """
    d = yyyymmdd(date)
    rid = remote_id.strip() if remote_id else ""
    if not rid and reference:
        rid = f"{_PREFIX}-PAY-{d}-{reference.strip()}"
    vch_open = '<VOUCHER VCHTYPE="Payment" ACTION="Create" OBJVIEW="Accounting Voucher View"'
    if rid:
        vch_open += f' REMOTEID="{xml_escape(rid)}"'
    vch_open += ">"
    parts = [
        vch_open,
        f"<DATE>{d}</DATE>",
        f"<EFFECTIVEDATE>{d}</EFFECTIVEDATE>",
        "<VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>",
        "<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>",
    ]
    if voucher_number.strip():
        parts.append(f"<VOUCHERNUMBER>{xml_escape(voucher_number.strip())}</VOUCHERNUMBER>")
    if reference.strip():
        parts.append(f"<REFERENCE>{xml_escape(reference.strip())}</REFERENCE>")
    if narration.strip():
        parts.append(f"<NARRATION>{xml_escape(narration.strip())}</NARRATION>")

    # Validate every amount up front (Decimal): missing, boolean,
    # non-finite or malformed amounts are refused before any XML is built.
    debit_amounts = [
        _decimal_amount(e.get("amount"), field=f"debit_entries[{i}].amount")
        for i, e in enumerate(debit_entries)
    ]
    # Double-entry balance check (tolerance 0.01, inclusive): signed entries
    # (debits negative, credit positive) must net to zero. Imbalance raises
    # ImbalanceError and no XML is produced -- never auto-corrected.
    _require_balanced(
        [-a for a in debit_amounts] + [sum(debit_amounts, Decimal(0))],
        what="Payment voucher")
    total_debit = sum(debit_amounts, Decimal(0))
    for idx, entry in enumerate(debit_entries):
        ledger = xml_escape(entry["ledger"].strip())
        amount = debit_amounts[idx]
        bill_ref = entry.get("bill_ref", "").strip()
        is_party = entry.get("is_party", bool(bill_ref))
        parts.append("<LEDGERENTRIES.LIST>")
        parts.append(f"<LEDGERNAME>{ledger}</LEDGERNAME>")
        parts.append("<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>")
        parts.append(f"<ISPARTYLEDGER>{'Yes' if is_party else 'No'}</ISPARTYLEDGER>")
        parts.append(f"<ISLASTDEEMEDPOSITIVE>{'Yes' if is_party else 'No'}</ISLASTDEEMEDPOSITIVE>")
        parts.append(f"<AMOUNT>{-amount:.2f}</AMOUNT>")
        if bill_ref:
            parts.append("<BILLALLOCATIONS.LIST>")
            parts.append(f"<NAME>{xml_escape(bill_ref)}</NAME>")
            parts.append("<BILLTYPE>Agst Ref</BILLTYPE>")
            parts.append(f"<AMOUNT>{-amount:.2f}</AMOUNT>")
            parts.append("</BILLALLOCATIONS.LIST>")
        parts.append("</LEDGERENTRIES.LIST>")

    # Credit: Cash/Bank (positive amount, DEEMED=No)
    parts.append("<LEDGERENTRIES.LIST>")
    parts.append(f"<LEDGERNAME>{xml_escape(credit_ledger.strip())}</LEDGERNAME>")
    parts.append("<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>")
    parts.append("<ISPARTYLEDGER>No</ISPARTYLEDGER>")
    parts.append("<ISLASTDEEMEDPOSITIVE>No</ISLASTDEEMEDPOSITIVE>")
    parts.append(f"<AMOUNT>{total_debit:.2f}</AMOUNT>")
    if bank_transaction_type.strip():
        parts.append("<BANKALLOCATIONS.LIST>")
        parts.append(f"<DATE>{d}</DATE>")
        parts.append(f"<INSTRUMENTDATE>{d}</INSTRUMENTDATE>")
        parts.append(f"<TRANSACTIONTYPE>{xml_escape(bank_transaction_type.strip())}</TRANSACTIONTYPE>")
        parts.append("<PAYMENTMODE>Transacted</PAYMENTMODE>")
        parts.append(f"<NAME>{_PREFIX}-{d}-{xml_escape(reference.strip() or 'PAY')}</NAME>")
        parts.append(f"<AMOUNT>{total_debit:.2f}</AMOUNT>")
        parts.append("</BANKALLOCATIONS.LIST>")
    parts.append("</LEDGERENTRIES.LIST>")

    parts.append("</VOUCHER>")
    return "".join(parts)


def build_receipt_voucher_message(
    date: str,
    credit_entries: list,
    debit_ledger: str = "Cash",
    reference: str = "",
    narration: str = "",
    voucher_number: str = "",
    remote_id: str = "",
    bank_transaction_type: str = "Others",
) -> str:
    """Build the <VOUCHER ...> TALLYMESSAGE body for a Receipt voucher.

    Mirror of payment: money comes IN.
    - Accounting voucher: VCHTYPE=Receipt, OBJVIEW=Accounting Voucher View,
      PERSISTEDVIEW=Accounting Voucher View. NO inventory lines.
    - Debit entry (Cash/Bank): LEDGERNAME + ISDEEMEDPOSITIVE=Yes +
      positive AMOUNT. NO ISPARTYLEDGER.
    - Credit entries (party/income): LEDGERNAME + ISDEEMEDPOSITIVE=No +
      negative AMOUNT. Party receipts get ISPARTYLEDGER=Yes +
      ISLASTDEEMEDPOSITIVE=Yes and BILLALLOCATIONS.LIST (NAME=bill ref,
      BILLTYPE=Agst Ref) for bill-wise settlement.
    - Net rule: debit(+) + credits(-) = 0.
    - EFFECTIVEDATE = DATE (both required for correct date display).

    Args:
        date: YYYYMMDD.
        credit_entries: list of dicts: {ledger, amount, bill_ref (optional),
            is_party (bool, default True for bill_ref entries)}.
        debit_ledger: Cash/Bank ledger name (default "Cash").
        reference: Receipt reference number.
        narration: Voucher narration.
        voucher_number: Voucher number hint (Tally may auto-number).
        remote_id: Explicit REMOTEID (auto-generated as BJB-RCT-<date>-<ref>
            if reference given and remote_id empty).
    """
    d = yyyymmdd(date)
    rid = remote_id.strip() if remote_id else ""
    if not rid and reference:
        rid = f"{_PREFIX}-RCT-{d}-{reference.strip()}"
    vch_open = '<VOUCHER VCHTYPE="Receipt" ACTION="Create" OBJVIEW="Accounting Voucher View"'
    if rid:
        vch_open += f' REMOTEID="{xml_escape(rid)}"'
    vch_open += ">"
    parts = [
        vch_open,
        f"<DATE>{d}</DATE>",
        f"<EFFECTIVEDATE>{d}</EFFECTIVEDATE>",
        "<VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>",
        "<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>",
    ]
    if voucher_number.strip():
        parts.append(f"<VOUCHERNUMBER>{xml_escape(voucher_number.strip())}</VOUCHERNUMBER>")
    if reference.strip():
        parts.append(f"<REFERENCE>{xml_escape(reference.strip())}</REFERENCE>")
    if narration.strip():
        parts.append(f"<NARRATION>{xml_escape(narration.strip())}</NARRATION>")

    # Validate every amount up front (Decimal): missing, boolean,
    # non-finite or malformed amounts are refused before any XML is built.
    credit_amounts = [
        _decimal_amount(e.get("amount"), field=f"credit_entries[{i}].amount")
        for i, e in enumerate(credit_entries)
    ]
    # Double-entry balance check (tolerance 0.01, inclusive): signed entries
    # (credits positive, debit negative) must net to zero.
    _require_balanced(
        [a for a in credit_amounts] + [-sum(credit_amounts, Decimal(0))],
        what="Receipt voucher")
    total_credit = sum(credit_amounts, Decimal(0))
    # Debit: Cash/Bank FIRST (negative amount, DEEMED=Yes)
    # (computed after credits so amounts net to zero)
    credit_parts = []
    for idx, entry in enumerate(credit_entries):
        ledger = xml_escape(entry["ledger"].strip())
        amount = credit_amounts[idx]
        bill_ref = entry.get("bill_ref", "").strip()
        is_party = entry.get("is_party", bool(bill_ref))
        credit_parts.append("<LEDGERENTRIES.LIST>")
        credit_parts.append(f"<LEDGERNAME>{ledger}</LEDGERNAME>")
        credit_parts.append("<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>")
        credit_parts.append(f"<ISPARTYLEDGER>{'Yes' if is_party else 'No'}</ISPARTYLEDGER>")
        credit_parts.append(f"<ISLASTDEEMEDPOSITIVE>{'Yes' if is_party else 'No'}</ISLASTDEEMEDPOSITIVE>")
        credit_parts.append(f"<AMOUNT>{amount:.2f}</AMOUNT>")
        if bill_ref:
            credit_parts.append("<BILLALLOCATIONS.LIST>")
            credit_parts.append(f"<NAME>{xml_escape(bill_ref)}</NAME>")
            credit_parts.append("<BILLTYPE>Agst Ref</BILLTYPE>")
            credit_parts.append(f"<AMOUNT>{amount:.2f}</AMOUNT>")
            credit_parts.append("</BILLALLOCATIONS.LIST>")
        credit_parts.append("</LEDGERENTRIES.LIST>")

    # Debit entry first in voucher
    parts.append("<LEDGERENTRIES.LIST>")
    parts.append(f"<LEDGERNAME>{xml_escape(debit_ledger.strip())}</LEDGERNAME>")
    parts.append("<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>")
    parts.append("<ISPARTYLEDGER>No</ISPARTYLEDGER>")
    parts.append("<ISLASTDEEMEDPOSITIVE>No</ISLASTDEEMEDPOSITIVE>")
    parts.append(f"<AMOUNT>{-total_credit:.2f}</AMOUNT>")
    if bank_transaction_type.strip():
        parts.append("<BANKALLOCATIONS.LIST>")
        parts.append(f"<DATE>{d}</DATE>")
        parts.append(f"<INSTRUMENTDATE>{d}</INSTRUMENTDATE>")
        parts.append(f"<TRANSACTIONTYPE>{xml_escape(bank_transaction_type.strip())}</TRANSACTIONTYPE>")
        parts.append("<PAYMENTMODE>Transacted</PAYMENTMODE>")
        parts.append(f"<NAME>{_PREFIX}-{d}-{xml_escape(reference.strip() or 'RCT')}</NAME>")
        parts.append(f"<AMOUNT>{-total_credit:.2f}</AMOUNT>")
        parts.append("</BANKALLOCATIONS.LIST>")
    parts.append("</LEDGERENTRIES.LIST>")
    parts.extend(credit_parts)

    parts.append("</VOUCHER>")
    return "".join(parts)


def build_contra_voucher_message(
    date: str,
    debit_ledger: str,
    credit_ledger: str,
    amount: float,
    reference: str = "",
    narration: str = "",
    voucher_number: str = "",
    remote_id: str = "",
    bank_transaction_type: str = "Others",
) -> str:
    """Build the <VOUCHER ...> TALLYMESSAGE body for a Contra voucher.

    Use for: cash deposited to bank, cash withdrawn from bank,
    bank-to-bank transfers. No party ledgers, no bill allocations.

    - Accounting voucher: VCHTYPE=Contra, OBJVIEW=Accounting Voucher View,
      PERSISTEDVIEW=Accounting Voucher View. NO inventory lines.
    - Debit entry (destination: where money goes TO): LEDGERNAME +
      ISDEEMEDPOSITIVE=Yes + NEGATIVE amount. NO ISPARTYLEDGER.
    - Credit entry (source: where money comes FROM): LEDGERNAME +
      ISDEEMEDPOSITIVE=No + POSITIVE amount. NO ISPARTYLEDGER.
    - Net rule: debit(-) + credit(+) = 0.
    - Sign rule (critical): DEEMED=Yes -> negative amount;
      DEEMED=No -> positive amount.
    - EFFECTIVEDATE = DATE.

    Args:
        date: YYYYMMDD.
        debit_ledger: Destination ledger (e.g. "PNB Bank A/c" for deposit).
        credit_ledger: Source ledger (e.g. "Cash" for deposit).
        amount: Positive transfer amount.
        reference: Contra reference number.
        narration: Voucher narration.
        voucher_number: Voucher number hint (Tally may auto-number).
        remote_id: Explicit REMOTEID (auto-generated as BJB-CTR-<date>-<ref>
            if reference given and remote_id empty).
        bank_transaction_type: e.g. "Others", "Cheque" -- adds
            BANKALLOCATIONS to the bank ledger entry (optional).
    """
    d = yyyymmdd(date)
    rid = remote_id.strip() if remote_id else ""
    if not rid and reference:
        rid = f"{_PREFIX}-CTR-{d}-{reference.strip()}"
    vch_open = '<VOUCHER VCHTYPE="Contra" ACTION="Create" OBJVIEW="Accounting Voucher View"'
    if rid:
        vch_open += f' REMOTEID="{xml_escape(rid)}"'
    vch_open += ">"
    parts = [
        vch_open,
        f"<DATE>{d}</DATE>",
        f"<EFFECTIVEDATE>{d}</EFFECTIVEDATE>",
        "<VOUCHERTYPENAME>Contra</VOUCHERTYPENAME>",
        "<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>",
    ]
    if voucher_number.strip():
        parts.append(f"<VOUCHERNUMBER>{xml_escape(voucher_number.strip())}</VOUCHERNUMBER>")
    if reference.strip():
        parts.append(f"<REFERENCE>{xml_escape(reference.strip())}</REFERENCE>")
    if narration.strip():
        parts.append(f"<NARRATION>{xml_escape(narration.strip())}</NARRATION>")

    # Validate the amount (Decimal) and the double-entry balance before
    # building anything. Signed entries (debit negative, credit positive)
    # must net to zero within the inclusive 0.01 tolerance.
    amt = _decimal_amount(amount, field="amount")
    _require_balanced([-amt, amt], what="Contra voucher")
    # Debit: destination (negative amount, DEEMED=Yes)
    parts.append("<LEDGERENTRIES.LIST>")
    parts.append(f"<LEDGERNAME>{xml_escape(debit_ledger.strip())}</LEDGERNAME>")
    parts.append("<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>")
    parts.append("<ISPARTYLEDGER>No</ISPARTYLEDGER>")
    parts.append("<ISLASTDEEMEDPOSITIVE>No</ISLASTDEEMEDPOSITIVE>")
    parts.append(f"<AMOUNT>{-amt:.2f}</AMOUNT>")
    if bank_transaction_type.strip():
        parts.append("<BANKALLOCATIONS.LIST>")
        parts.append(f"<DATE>{d}</DATE>")
        parts.append(f"<INSTRUMENTDATE>{d}</INSTRUMENTDATE>")
        parts.append(f"<TRANSACTIONTYPE>{xml_escape(bank_transaction_type.strip())}</TRANSACTIONTYPE>")
        parts.append("<PAYMENTMODE>Transacted</PAYMENTMODE>")
        parts.append(f"<NAME>{_PREFIX}-{d}-{xml_escape(reference.strip() or 'CTR')}</NAME>")
        parts.append(f"<AMOUNT>{-amt:.2f}</AMOUNT>")
        parts.append("</BANKALLOCATIONS.LIST>")
    parts.append("</LEDGERENTRIES.LIST>")
    # Credit: source (positive amount, DEEMED=No)
    parts.append("<LEDGERENTRIES.LIST>")
    parts.append(f"<LEDGERNAME>{xml_escape(credit_ledger.strip())}</LEDGERNAME>")
    parts.append("<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>")
    parts.append("<ISPARTYLEDGER>No</ISPARTYLEDGER>")
    parts.append("<ISLASTDEEMEDPOSITIVE>No</ISLASTDEEMEDPOSITIVE>")
    parts.append(f"<AMOUNT>{amt:.2f}</AMOUNT>")
    parts.append("</LEDGERENTRIES.LIST>")

    parts.append("</VOUCHER>")
    return "".join(parts)


def build_journal_voucher_message(
    date: str,
    debit_entries: list,
    credit_entries: list,
    reference: str = "",
    narration: str = "",
    voucher_number: str = "",
    remote_id: str = "",
) -> str:
    """Build the <VOUCHER ...> TALLYMESSAGE body for a Journal voucher.

    Use for: adjustments with NO cash/bank involvement -- depreciation,
    outstanding expenses, provisions, reclassifications, party-to-party
    transfers. Multiple debits and credits allowed (compound journal).

    - Accounting voucher: VCHTYPE=Journal, OBJVIEW=Accounting Voucher View,
      PERSISTEDVIEW=Accounting Voucher View. NO inventory lines.
    - Debit entry: LEDGERNAME + ISDEEMEDPOSITIVE=Yes + NEGATIVE amount.
    - Credit entry: LEDGERNAME + ISDEEMEDPOSITIVE=No + POSITIVE amount.
    - Net rule: sum of all signed amounts MUST be 0 (balanced).
    - Sign rule (critical): DEEMED=Yes -> negative amount;
      DEEMED=No -> positive amount.
    - Cash/Bank ledgers excluded by design (Tally blocks them in journals
      unless F12 "Allow Cash Accounts in Journals" is enabled).
    - BILLALLOCATIONS supported per party ledger entry:
      {"bill_name": str, "bill_type": "New Ref"|"Agst Ref"|"On Account"}.
    - EFFECTIVEDATE = DATE.

    Args:
        date: YYYYMMDD.
        debit_entries: List of {"ledger": str, "amount": float,
            "bill_allocations": [{"bill_name": str,
            "bill_type": str}]} -- amounts positive, converted to negative.
        credit_entries: Same shape -- amounts positive, stay positive.
        reference: Journal reference number.
        narration: Voucher narration.
        voucher_number: Voucher number hint (Tally may auto-number).
        remote_id: Explicit REMOTEID (auto-generated as BJB-JRN-<date>-<ref>
            if reference given and remote_id empty).
    """
    d = yyyymmdd(date)
    rid = remote_id.strip() if remote_id else ""
    if not rid and reference:
        rid = f"{_PREFIX}-JRN-{d}-{reference.strip()}"
    vch_open = '<VOUCHER VCHTYPE="Journal" ACTION="Create" OBJVIEW="Accounting Voucher View"'
    if rid:
        vch_open += f' REMOTEID="{xml_escape(rid)}"'
    vch_open += ">"
    parts = [
        vch_open,
        f"<DATE>{d}</DATE>",
        f"<EFFECTIVEDATE>{d}</EFFECTIVEDATE>",
        "<VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>",
        "<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>",
        "<HASCASHFLOW>No</HASCASHFLOW>",
    ]
    if voucher_number.strip():
        parts.append(f"<VOUCHERNUMBER>{xml_escape(voucher_number.strip())}</VOUCHERNUMBER>")
    if reference.strip():
        parts.append(f"<REFERENCE>{xml_escape(reference.strip())}</REFERENCE>")
    if narration.strip():
        parts.append(f"<NARRATION>{xml_escape(narration.strip())}</NARRATION>")

    # Validate every amount up front (Decimal): missing, boolean,
    # non-finite or malformed amounts are refused before any XML is built.
    debit_amts = [
        _decimal_amount(e.get("amount"), field=f"debit_entries[{i}].amount")
        for i, e in enumerate(debit_entries or [])
    ]
    credit_amts = [
        _decimal_amount(e.get("amount"), field=f"credit_entries[{i}].amount")
        for i, e in enumerate(credit_entries or [])
    ]
    # Double-entry balance check (tolerance 0.01, inclusive): signed entries
    # (debits negative, credits positive) must net to zero. Imbalance raises
    # ImbalanceError and no XML is produced -- never auto-corrected.
    _require_balanced(
        [-a for a in debit_amts] + [a for a in credit_amts],
        what="Journal voucher")

    def _ledger_block(ledger: str, signed_amt, bills: list) -> list:
        # signed_amt: negative for debit, positive for credit
        is_debit = signed_amt < 0
        blk = ["<LEDGERENTRIES.LIST>"]
        blk.append(f"<LEDGERNAME>{xml_escape(ledger.strip())}</LEDGERNAME>")
        blk.append(f"<ISDEEMEDPOSITIVE>{'Yes' if is_debit else 'No'}</ISDEEMEDPOSITIVE>")
        blk.append("<ISPARTYLEDGER>Yes</ISPARTYLEDGER>" if bills else "<ISPARTYLEDGER>No</ISPARTYLEDGER>")
        blk.append("<ISLASTDEEMEDPOSITIVE>No</ISLASTDEEMEDPOSITIVE>")
        blk.append(f"<AMOUNT>{signed_amt:.2f}</AMOUNT>")
        for b in bills or []:
            blk.append("<BILLALLOCATIONS.LIST>")
            blk.append(f"<NAME>{xml_escape(str(b.get('bill_name', '')))}</NAME>")
            blk.append(f"<BILLTYPE>{xml_escape(str(b.get('bill_type', 'New Ref')))}</BILLTYPE>")
            blk.append(f"<AMOUNT>{signed_amt:.2f}</AMOUNT>")
            blk.append("</BILLALLOCATIONS.LIST>")
        blk.append("</LEDGERENTRIES.LIST>")
        return blk

    for i, e in enumerate(debit_entries or []):
        parts.extend(_ledger_block(
            e.get("ledger", ""), -abs(debit_amts[i]),
            e.get("bill_allocations", [])))
    for i, e in enumerate(credit_entries or []):
        parts.extend(_ledger_block(
            e.get("ledger", ""), abs(credit_amts[i]),
            e.get("bill_allocations", [])))

    parts.append("</VOUCHER>")
    return "".join(parts)


def build_voucher_delete_message(voucher_type: str, remote_id: str = "",
                                 date: str = "", voucher_number: str = "") -> str:
    """Build <VOUCHER ... ACTION="Delete"> body for true voucher deletion.
    Verified 2026-10-03: GST-TEST-003 (Purchase No. 13) -> DELETED=1, ERRORS=0.
    - UPPERCASE ACTION="Delete" is mandatory (lowercase silently fails).
    - remote_id is the reliable identifier -- tool-created vouchers carry an
      explicit REMOTEID (e.g. {_PREFIX}-PUR-20260401-GST-TEST-003"). UI-created
      vouchers get Tally-internal IDs that can't be targeted via XML.
    """
    vt = xml_escape(voucher_type.strip())
    parts = [f'<VOUCHER VCHTYPE="{vt}" ACTION="Delete"']
    if remote_id.strip():
        parts.append(f' REMOTEID="{xml_escape(remote_id.strip())}"')
    parts.append(">")
    if date.strip():
        parts.append(f"<DATE>{yyyymmdd(date.strip())}</DATE>")
    parts.append(f"<VOUCHERTYPENAME>{vt}</VOUCHERTYPENAME>")
    if voucher_number.strip():
        parts.append(f"<VOUCHERNUMBER>{xml_escape(voucher_number.strip())}</VOUCHERNUMBER>")
    parts.append("</VOUCHER>")
    return "".join(parts)


def wrap_import_envelope(message_xml: str, company: Optional[str] = None) -> str:
    """Wrap a TALLYMESSAGE body in the Import Data envelope.

    The envelope pins <SVCURRENTCOMPANY> so the import always targets the
    intended company. company=None resolves to the validated TALLY_COMPANY
    (fail-closed when unset).
    """
    comp = company if company is not None else get_pinned_company()
    return _IMPORT_ENVELOPE.format(body=message_xml, company=xml_escape(comp))


# ---------------------------------------------------------------------------
# mutation safety gate -- preview-before-write with confirm_token
# ---------------------------------------------------------------------------
#
# Two-step flow (enforced in code, not just convention):
#   1. preview=True  -> build the exact XML, return it plus a short-lived,
#                      single-use confirm_token bound to (operation, company,
#                      exact XML bytes, optional extra binding such as the
#                      validated delete target). NOTHING is sent to Tally.
#   2. preview=False + confirm_token -> the gate re-verifies the binding and
#      expiry, atomically consumes the token (one dispatch max), rebuilds the
#      payload from the same arguments and only then POSTs it.
#
# A token proves that an exact preview occurred; it does NOT prove a human
# reviewed it. Tokens are server-issued, unpredictable, single-use, expire
# after _GATE_TTL_SECONDS, and never leave this process (in-memory store).
# A server restart invalidates outstanding tokens.

_GATE_TTL_SECONDS = 900  # 15 minutes
_GATE_MAX_TOKENS = 2048


def _payload_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MutationGate:
    """Issues and atomically consumes single-use mutation confirm_tokens."""

    def __init__(self, ttl_seconds: int = _GATE_TTL_SECONDS,
                 max_tokens: int = _GATE_MAX_TOKENS):
        self._ttl = ttl_seconds
        self._max = max_tokens
        self._lock = threading.Lock()
        self._tokens: dict = {}

    def _cleanup_locked(self) -> None:
        now = _time_mod.time()
        expired = [t for t, rec in self._tokens.items()
                   if rec["expires_at"] <= now]
        for t in expired:
            del self._tokens[t]
        # Bound storage: if still over capacity, drop the oldest.
        while len(self._tokens) > self._max:
            oldest = min(self._tokens, key=lambda t: self._tokens[t]["issued_at"])
            del self._tokens[oldest]

    def issue(self, op: str, company: str, payload_hash: str,
              label: str = "", extra: Optional[dict] = None) -> tuple:
        """Mint a token bound to this exact mutation. Returns (token, expires_at)."""
        token = secrets.token_urlsafe(32)
        now = _time_mod.time()
        with self._lock:
            self._tokens[token] = {
                "op": op,
                "company": company,
                "payload_hash": payload_hash,
                "label": label,
                "extra": dict(extra or {}),
                "issued_at": now,
                "expires_at": now + self._ttl,
                "used": False,
            }
            self._cleanup_locked()
        return token, now + self._ttl

    def consume(self, op: str, company: str, payload_hash: str,
                token: str, extra: Optional[dict] = None) -> None:
        """Atomically verify and single-use-consume a token.

        Raises MutationTokenError on any mismatch. At most one caller can
        successfully consume a given token, so concurrent reuse results in
        at most one dispatch.
        """
        if not token:
            raise MutationTokenError(
                "Missing confirm_token. Preview the mutation first "
                "(preview=True), then execute with preview=False and the "
                "confirm_token returned by the preview."
            )
        with self._lock:
            self._cleanup_locked()
            rec = self._tokens.get(token)
            if rec is None:
                raise MutationTokenError(
                    "Unknown or expired confirm_token. Preview the mutation "
                    "again to get a fresh token."
                )
            if rec["used"]:
                raise MutationTokenError(
                    "This confirm_token was already used. Tokens are "
                    "single-use: preview again for a new token."
                )
            if rec["op"] != op:
                raise MutationTokenError(
                    f"Token was issued for '{rec['op']}', not '{op}'. "
                    "Tokens cannot be reused across operations."
                )
            if rec["company"] != company:
                raise MutationTokenError(
                    "Token was issued for a different company "
                    f"('{rec['company']}'). Refusing cross-company execution."
                )
            if rec["payload_hash"] != payload_hash:
                raise MutationTokenError(
                    "Arguments changed since the preview: the rebuilt payload "
                    "no longer matches the previewed one. Preview again."
                )
            want_extra = dict(extra or {})
            if rec["extra"] != want_extra:
                raise MutationTokenError(
                    "Validation binding changed since the preview "
                    "(e.g. the delete target changed). Preview again."
                )
            rec["used"] = True


# Process-wide gate. Tokens live in memory only: a server restart
# invalidates outstanding tokens (fail-closed).
_MUTATION_GATE = MutationGate()


# ---------------------------------------------------------------------------
# safe XML response parsing -- real parser, no regex extraction
# ---------------------------------------------------------------------------
#
# Tally responses are parsed with xml.etree.ElementTree. DTD/entity
# declarations are rejected outright (no internal or external entity
# expansion, so no billion-laughs / XXE), CDATA sections and standard
# entities are handled by the parser. Malformed XML raises instead of
# silently producing a "success".

def _parse_xml_safe(text: str):
    """Parse Tally XML safely. Raises UnsafeXmlError/XmlParseError."""
    if not text or not text.strip():
        raise XmlParseError("Empty response from Tally.")
    head = text.lstrip()[:200].upper()
    if "<!DOCTYPE" in text.upper():
        raise UnsafeXmlError(
            "Response contains a DOCTYPE declaration; refusing to parse "
            "(entity expansion disabled for safety)."
        )
    try:
        return _ET.fromstring(text)
    except _ET.ParseError as e:
        raise XmlParseError(f"Malformed XML in Tally response: {e}")


def _local(tag: str) -> str:
    """Strip any namespace prefix: '{ns}NAME' -> 'NAME', 'p:NAME' -> 'NAME'."""
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    return tag


def _find_all_by_local(root, name: str) -> list:
    return [el for el in root.iter() if _local(el.tag) == name]


def _first_text(root, name: str) -> str:
    for el in root.iter():
        if _local(el.tag) == name:
            return (el.text or "").strip()
    return ""


# ---------------------------------------------------------------------------
# Decimal amount validation + double-entry balance check
# ---------------------------------------------------------------------------
#
# Signed-amount convention (Tally XML): debit entries carry a NEGATIVE
# <AMOUNT> with ISDEEMEDPOSITIVE=Yes; credit entries carry a POSITIVE
# <AMOUNT> with ISDEEMEDPOSITIVE=No. A balanced voucher sums to zero.
# Tolerance is 0.01 and the boundary is INCLUSIVE: |total| <= 0.01 passes,
# anything larger fails. Nothing is auto-corrected: on imbalance the
# builder raises and no XML is produced, so no mutation is attempted.

_BALANCE_TOLERANCE = Decimal("0.01")


def _decimal_amount(value, field: str = "amount") -> Decimal:
    """Validate one amount. Rejects missing/bool/non-finite/malformed values."""
    if value is None or isinstance(value, bool):
        raise ValueError(f"Invalid {field}: {value!r} (missing or boolean).")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError(f"Invalid {field}: {value!r} (not a number).")
    if not d.is_finite():
        raise ValueError(f"Invalid {field}: {value!r} (non-finite).")
    return d


def _require_balanced(signed_amounts, what: str = "voucher") -> Decimal:
    """Require signed ledger-entry amounts to net to zero within tolerance.

    Returns the net total. Raises ImbalanceError otherwise; callers must
    not build or post XML when this raises.
    """
    total = sum(signed_amounts, Decimal(0))
    if abs(total) > _BALANCE_TOLERANCE:
        raise ImbalanceError(
            f"{what} is out of balance: signed entries net to {total} "
            f"(tolerance {_BALANCE_TOLERANCE}, inclusive). Refusing to build."
        )
    return total


# ---------------------------------------------------------------------------
# sync -- master data snapshot for fast existence checks
# ---------------------------------------------------------------------------

import json as _json
import time as _time
import os as _os

_SYNC_DIR = _os.path.join(_os.path.dirname(__file__), "sync")
_SYNC_FILE = _os.path.join(_SYNC_DIR, "masters.json")
_SYNC_LOCK_FILE = _os.path.join(_SYNC_DIR, "masters.lock")
_SYNC_MAX_AGE_HOURS = 4  # auto-refresh if older than this
# Cache policy (documented): entries are per-company and considered fresh for
# up to 4 hours. Destructive operations (deletes) NEVER use this cache --
# they always do fresh exports. Any successful Tally mutation invalidates the
# company's cache entry so the next check re-syncs.

try:
    import fcntl as _fcntl
except ImportError:  # Windows: no fcntl; lock degrades to best-effort
    _fcntl = None


class _SyncLock:
    """Best-effort inter-process lock for the masters cache file."""

    def __init__(self):
        self._fh = None

    def __enter__(self):
        try:
            _sync_ensure_dir()
            self._fh = open(_SYNC_LOCK_FILE, "w")
            if _fcntl is not None:
                _fcntl.flock(self._fh.fileno(), _fcntl.LOCK_EX)
        except OSError:
            self._fh = None
        return self

    def __exit__(self, *exc):
        try:
            if self._fh is not None:
                if _fcntl is not None:
                    _fcntl.flock(self._fh.fileno(), _fcntl.LOCK_UN)
                self._fh.close()
        except OSError:
            pass
        self._fh = None
        return False


def _sync_ensure_dir():
    _os.makedirs(_SYNC_DIR, exist_ok=True)


def _sync_bucket_key() -> str:
    """Cache bucket for the current company ("" when TALLY_COMPANY unset)."""
    try:
        return get_pinned_company()
    except CompanyNotConfiguredError:
        return ""


def _sync_load():
    """Load this company's cached masters.

    Returns (bucket_dict, age_hours) or (None, None). Legacy (pre
    company-scoped) files and corrupt files are treated as a cache miss so
    the caller re-syncs; they are never trusted.
    """
    try:
        with open(_SYNC_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if not isinstance(data, dict):
            return None, None
        bucket = data.get("companies", {}).get(_sync_bucket_key())
        if not isinstance(bucket, dict):
            return None, None
        ts = bucket.get("_synced_at", 0)
        age_h = (_time.time() - ts) / 3600 if ts else 999
        return bucket, age_h
    except (FileNotFoundError, _json.JSONDecodeError, OSError,
            AttributeError, TypeError):
        return None, None


def _sync_save(ledgers, stock_items):
    """Atomically save this company's masters (temp file + rename, locked)."""
    _sync_ensure_dir()
    bucket = {
        "_synced_at": _time.time(),
        "_synced_at_str": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "ledgers": ledgers,
        "stock_items": stock_items,
    }
    with _SyncLock():
        try:
            with open(_SYNC_FILE, "r", encoding="utf-8") as f:
                data = _json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (FileNotFoundError, _json.JSONDecodeError, OSError):
            data = {}
        companies = data.get("companies")
        if not isinstance(companies, dict):
            companies = {}
        companies[_sync_bucket_key()] = bucket
        data["companies"] = companies
        tmp = _SYNC_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                _os.fsync(f.fileno())
            except OSError:
                pass
        _os.replace(tmp, _SYNC_FILE)
    return bucket


def _sync_invalidate_company(company: str) -> None:
    """Drop one company's cache bucket after a successful mutation.

    Best-effort: failures are swallowed by the caller. The next existence
    check will re-sync from Tally.
    """
    with _SyncLock():
        try:
            with open(_SYNC_FILE, "r", encoding="utf-8") as f:
                data = _json.load(f)
        except (FileNotFoundError, _json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        companies = data.get("companies")
        if isinstance(companies, dict) and company in companies:
            del companies[company]
            tmp = _SYNC_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
            _os.replace(tmp, _SYNC_FILE)


def _do_sync(client):
    """Fetch fresh masters from Tally and save. Returns data dict."""
    ledgers = []
    try:
        r = client.list_ledgers()
        lr = r if isinstance(r, dict) else {}
        for lg in lr.get("ledgers", []):
            ledgers.append({
                "name": lg.get("name", ""),
                "parent": lg.get("parent", ""),
            })
    except (requests.RequestException, ValueError, KeyError, TypeError,
            AttributeError):
        # Best-effort sync: a failed fetch just leaves this section empty.
        # Programming errors (other exception types) still propagate.
        pass
    stock_items = []
    try:
        r = client.get_stock_summary()
        sr = r if isinstance(r, dict) else {}
        for it in sr.get("items", []):
            stock_items.append({"name": it.get("name", "")})
    except (requests.RequestException, ValueError, KeyError, TypeError,
            AttributeError):
        pass
    return _sync_save(ledgers, stock_items)


def _ensure_fresh(client, force=False):
    """Return (data, was_refreshed). Auto-sync if stale or missing."""
    data, age_h = _sync_load()
    if force or data is None or age_h > _SYNC_MAX_AGE_HOURS:
        data = _do_sync(client)
        return data, True
    return data, False


# ---------------------------------------------------------------------------
# response parsing -- never trust HTTP 200 alone
# ---------------------------------------------------------------------------

@dataclass
class ImportResult:
    success: bool
    action: str            # Create | Alter | Delete
    object_type: str       # Ledger | Group
    name: str
    created: int = 0
    altered: int = 0
    deleted: int = 0
    errors: int = 0
    line_errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    preview: bool = False
    xml: str = ""
    message: str = ""
    company: str = ""                 # pinned TALLY_COMPANY the payload targets
    confirm_token: str = ""           # preview only: single-use execute token
    confirm_token_expires_at: float = 0.0  # unix timestamp

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "action": self.action,
            "object_type": self.object_type,
            "name": self.name,
            "created": self.created,
            "altered": self.altered,
            "deleted": self.deleted,
            "errors": self.errors,
            "line_errors": self.line_errors,
            "warnings": self.warnings,
            "preview": self.preview,
            "message": self.message,
            "xml": self.xml,
            "company": self.company,
            "confirm_token": self.confirm_token,
            "confirm_token_expires_at": self.confirm_token_expires_at,
        }


def _parse_import_counters(text: str):
    """Extract CREATED/ALTERED/DELETED/ERRORS ints + LINEERROR list via XML parsing.

    Returns (counters_dict, line_errors). Raises UnsafeXmlError/XmlParseError
    on hostile or malformed responses -- never silently misreads.
    """
    root = _parse_xml_safe(text)

    def _int(name: str) -> int:
        for el in root.iter():
            if _local(el.tag) == name:
                raw = (el.text or "").strip()
                try:
                    return int(raw)
                except ValueError:
                    return 0
        return 0

    line_errors = []
    for el in _find_all_by_local(root, "LINEERROR"):
        msg = "".join(el.itertext()).strip()
        if msg:
            line_errors.append(msg)
    return (
        {
            "CREATED": _int("CREATED"),
            "ALTERED": _int("ALTERED"),
            "DELETED": _int("DELETED"),
            "ERRORS": _int("ERRORS"),
        },
        line_errors,
    )


def _tag_int(text: str, tag: str) -> int:
    """Legacy helper kept for compatibility; parses via the safe XML parser."""
    try:
        counters, _ = _parse_import_counters(text)
    except (UnsafeXmlError, XmlParseError):
        return 0
    return counters.get(tag, 0)


def parse_import_response(text: str, action: str, object_type: str, name: str,
                          xml: str = "") -> ImportResult:
    """Parse a Tally Import Data response. Success is derived ONLY from the
    CREATED / ALTERED / DELETED / ERRORS / LINEERROR fields, extracted with
    a real XML parser (CDATA, entities, repeated elements and namespaces
    handled; DTD/entity declarations rejected).

    Malformed or hostile responses yield success=False -- never false success.
    """
    created = _tag_int(text, "CREATED")
    altered = _tag_int(text, "ALTERED")
    deleted = _tag_int(text, "DELETED")
    errors = _tag_int(text, "ERRORS")

    try:
        counters, line_errors = _parse_import_counters(text)
    except (UnsafeXmlError, XmlParseError) as e:
        return ImportResult(
            success=False, action=action, object_type=object_type, name=name,
            xml=xml,
            message=(f"{object_type} '{name}' {action.lower()} FAILED: "
                     f"could not parse Tally response ({e})."),
        )
    created = counters["CREATED"]
    altered = counters["ALTERED"]
    deleted = counters["DELETED"]
    errors = counters["ERRORS"]

    if action == "Create":
        ok = created >= 1 and errors == 0 and not line_errors
        detail = f"created={created}"
    elif action == "Alter":
        ok = altered >= 1 and errors == 0 and not line_errors
        detail = f"altered={altered}"
    elif action == "Delete":
        ok = deleted >= 1 and errors == 0 and not line_errors
        detail = f"deleted={deleted}"
    else:
        ok, detail = False, "unknown action"

    if ok:
        message = f"{object_type} '{name}' {action.lower()}d successfully ({detail})."
    else:
        reason = "; ".join(line_errors) if line_errors else f"errors={errors}, {detail}"
        message = f"{object_type} '{name}' {action.lower()} FAILED: {reason}"

    return ImportResult(
        success=ok,
        action=action,
        object_type=object_type,
        name=name,
        created=created,
        altered=altered,
        deleted=deleted,
        errors=errors,
        line_errors=line_errors,
        xml=xml,
        message=message,
    )


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

class TallyClient:
    """Minimal HTTP client for Tally Prime's XML API on port 9999."""

    def __init__(self, base_url: str = DEFAULT_TALLY_URL,
                 timeout: int = DEFAULT_TIMEOUT,
                 gate: Optional["MutationGate"] = None):
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        # Mutation safety gate (confirm_token). Defaults to the process-wide
        # gate; tests may inject their own.
        self._gate = gate if gate is not None else _MUTATION_GATE

    # -- low level --------------------------------------------------------

    def check_connection(self) -> dict:
        """Read-only liveness check: GET / on port 9999."""
        try:
            r = requests.get(self.base_url, timeout=min(self.timeout, 10))
            return {
                "reachable": r.status_code == 200,
                "http_status": r.status_code,
                "url": self.base_url,
            }
        except requests.RequestException as e:
            return {"reachable": False, "url": self.base_url,
                    "error": f"{type(e).__name__}: {e}"}

    def _mutation_refused(self, action: str, object_type: str, name: str,
                            message: str, xml: str = "",
                            company: str = "") -> ImportResult:
        """Build a failure ImportResult for a mutation that was NOT sent."""
        return ImportResult(
            success=False, action=action, object_type=object_type, name=name,
            xml=xml, company=company, message=message,
        )

    def import_masters(self, message_xml: str, action: str, object_type: str,
                       name: str, preview: bool = True,
                       confirm_token: str = "") -> ImportResult:
        """Wrap message XML in the company-pinned Import Data envelope.

        Safety gate (enforced, not just convention):
        - preview=True (default): build the exact XML, return it plus a
          short-lived single-use confirm_token. NOTHING is sent to Tally.
        - preview=False: requires the confirm_token from a prior preview of
          the same operation/company/payload. The token is verified and
          atomically consumed; any mismatch fails with zero HTTP requests.
        """
        try:
            company = get_pinned_company()
        except CompanyNotConfiguredError as e:
            return self._mutation_refused(
                action, object_type, name, f"REFUSED: {e}")
        envelope = wrap_import_envelope(message_xml, company)
        return self._gated_send(
            envelope, action, object_type, name, company,
            preview=preview, confirm_token=confirm_token)

    def _gated_send(self, envelope: str, action: str, object_type: str,
                    name: str, company: str, preview: bool,
                    confirm_token: str, op: Optional[str] = None,
                    extra: Optional[dict] = None,
                    label: str = "") -> ImportResult:
        """Shared preview/token gate for every Tally-bound mutation.

        extra binds additional validation state (e.g. the verified delete
        target) into the token; it must match at consume time.
        """
        op = op or f"{action}:{object_type}"
        label = label or f"{action} {object_type} '{name}'"
        phash = _payload_hash(envelope)
        if preview:
            token, exp = self._gate.issue(op, company, phash, label, extra)
            return ImportResult(
                success=True, action=action, object_type=object_type, name=name,
                preview=True, xml=envelope, company=company,
                confirm_token=token, confirm_token_expires_at=exp,
                message=(
                    f"PREVIEW only -- nothing sent to Tally. "
                    f"Would {action.lower()} {object_type} '{name}' "
                    f"in company '{company}'. To execute, call again with "
                    f"preview=False and the confirm_token from this preview. "
                    f"The token is single-use and expires at unix time "
                    f"{exp:.0f}. A token proves this exact preview ran; it "
                    f"does not prove a human reviewed it."
                ),
            )
        try:
            self._gate.consume(op, company, phash, confirm_token, extra)
        except MutationTokenError as e:
            # No HTTP request is made on token failure.
            return self._mutation_refused(
                action, object_type, name, f"REFUSED: {e}",
                xml=envelope, company=company)
        return self._post_import(envelope, action, object_type, name, company)

    def _post_import(self, envelope: str, action: str, object_type: str,
                     name: str, company: str = "") -> ImportResult:
        """POST an already-authorized envelope. No gate check here -- callers
        must have consumed a valid token (or be a read path). Never retries:
        on transport failure the caller must reconcile and re-preview."""
        try:
            r = requests.post(
                self.base_url,
                data=envelope.encode("utf-8"),
                headers={"Content-Type": "text/xml"},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            return ImportResult(
                success=False, action=action, object_type=object_type, name=name,
                xml=envelope, company=company,
                message=(f"Connection to Tally failed ({type(e).__name__}: {e}). "
                         f"Is Tally Prime running with port 9999 enabled? "
                         f"No automatic retry was attempted; reconcile in "
                         f"Tally, then preview again if needed."),
            )
        # NOTE: HTTP 200 means nothing by itself -- parse the body.
        res = parse_import_response(r.text, action, object_type, name,
                                    xml=envelope)
        res.company = company
        if res.success:
            # Invalidate the local masters cache for this company so later
            # existence checks see fresh data (best effort, never fatal).
            try:
                _sync_invalidate_company(company)
            except Exception:
                pass
        return res

    # -- deletion target validation ---------------------------------------
    #
    # Before previewing (and again immediately before executing) a delete,
    # the exact target is re-queried from the SAME pinned company with a
    # fresh export -- never the local sync cache. Exact match only; missing
    # or ambiguous targets fail closed. The verified target identity is
    # bound into the confirm_token and re-checked before dispatch.
    #
    # Limitation: Tally's XML API has no transactional protection, so a
    # target could theoretically change between the final re-check and the
    # POST. The window is kept minimal (re-check immediately precedes the
    # dispatch) and any Tally-side refusal surfaces as ERRORS/LINEERROR.

    def _fresh_export_text(self, collection_id: str, tally_type: str,
                           fields: list, company: str) -> str:
        env = build_collection_export_envelope(
            collection_id, tally_type, fields, company=company)
        try:
            r = requests.post(
                self.base_url, data=env.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout)
        except requests.RequestException as e:
            raise ConnectionError(
                f"Cannot reach Tally for delete-target validation: {e}")
        return r.text

    def _parse_group_parents(self, text: str) -> list:
        """Parse <GROUP NAME=..><PARENT>..</PARENT></GROUP> pairs via XML."""
        root = _parse_xml_safe(text)
        out = []
        for el in _find_all_by_local(root, "GROUP"):
            gname = (el.get("NAME") or "").strip()
            parent = ""
            for child in el:
                if _local(child.tag) == "PARENT":
                    parent = "".join(child.itertext()).strip()
                    break
            if gname:
                out.append({"name": gname, "parent": parent})
        return out

    def _validate_exact_master(self, object_type: str, name: str,
                               company: str) -> tuple:
        """Validate one delete target. Returns (target_id, dep_note, error).

        error is "" on success. target_id is the exact name as Tally
        returned it; dep_note describes the dependency check performed.
        """
        want = (name or "").strip()
        if not want:
            return "", "", (
                f"REFUSED: empty {object_type.lower()} name -- nothing to delete.")
        spec = {
            "Ledger": ("ListLedger", "Ledger"),
            "Group": ("ListGroup", "Group"),
            "VoucherType": ("ListVoucherType", "VoucherType"),
            "Godown": ("ListGodown", "Godown"),
            "Unit": ("ListUnit", "Unit"),
            "StockGroup": ("ListStockGroup", "StockGroup"),
            "StockCategory": ("ListStockCategory", "StockCategory"),
            "StockItem": ("ListStockItem", "StockItem"),
        }.get(object_type)
        if not spec:
            return "", "", f"REFUSED: no target validator for {object_type}."
        coll_id, tally_type = spec
        fields = ["Name", "Parent"] if object_type in ("Ledger", "Group") else ["Name"]
        try:
            text = self._fresh_export_text(coll_id, tally_type, fields, company)
            if object_type == "Ledger":
                items = parse_ledger_parents(text)
            elif object_type == "Group":
                items = self._parse_group_parents(text)
            else:
                items = [{"name": n} for n in parse_collection_names(text)]
        except (ConnectionError, UnsafeXmlError, XmlParseError) as e:
            return "", "", (
                f"REFUSED: could not validate the delete target ({e}). "
                f"Failing closed -- no delete attempted.")
        names = [it["name"] for it in items]
        hits = [n for n in names if n == want]
        if not hits:
            return "", "", (
                f"REFUSED: {object_type} '{want}' not found in company "
                f"'{company}' (exact match required).")
        if len(hits) > 1:
            return "", "", (
                f"REFUSED: {len(hits)} records exactly match '{want}' -- "
                f"ambiguous target. Refine the name.")
        dep_note = ("Dependency check: Tally's XML API exposes no cheap "
                    "cross-reference query for this type; Tally itself "
                    "refuses the delete (ERRORS/LINEERROR) if the record is "
                    "in use.")
        if object_type == "Group":
            # Refuse non-empty groups: ledgers or sub-groups underneath.
            try:
                led_text = self._fresh_export_text(
                    "ListLedger", "Ledger", ["Name", "Parent"], company)
                ledgers = parse_ledger_parents(led_text)
            except (ConnectionError, UnsafeXmlError, XmlParseError) as e:
                return "", "", (
                    f"REFUSED: could not check group contents ({e}). "
                    f"Failing closed.")
            holders = [l["name"] for l in ledgers if l["group"] == want]
            sub = [it["name"] for it in items if it["parent"] == want]
            blockers = holders + sub
            if blockers:
                return "", "", (
                    f"REFUSED: group '{want}' is not empty "
                    f"(ledgers/sub-groups: {', '.join(blockers[:8])}"
                    f"{'...' if len(blockers) > 8 else ''}). "
                    f"Empty it first.")
            dep_note = ("Verified empty: no ledgers and no sub-groups under "
                        f"'{want}'.")
        return want, dep_note, ""

    def _delete_master(self, object_type: str, name: str, build_envelope_fn,
                       validate_fn, preview: bool,
                       confirm_token: str) -> ImportResult:
        """Shared delete flow: validate -> gate -> re-validate -> dispatch."""
        try:
            company = get_pinned_company()
        except CompanyNotConfiguredError as e:
            return self._mutation_refused(
                "Delete", object_type, name, f"REFUSED: {e}")
        target_id, dep_note, err = validate_fn(company)
        if err:
            return self._mutation_refused(
                "Delete", object_type, name, err, company=company)
        envelope = build_envelope_fn(company)
        op = f"Delete:{object_type}"
        extra = {"target": target_id}
        if preview:
            token, exp = self._gate.issue(
                op, company, _payload_hash(envelope),
                f"Delete {object_type} '{name}'", extra)
            return ImportResult(
                success=True, action="Delete", object_type=object_type,
                name=name, preview=True, xml=envelope, company=company,
                confirm_token=token, confirm_token_expires_at=exp,
                message=(
                    f"PREVIEW only -- nothing sent to Tally. Would delete "
                    f"{object_type} '{target_id}' in company '{company}'. "
                    f"Target verified by fresh export. {dep_note} "
                    f"To execute, call again with preview=False and the "
                    f"confirm_token from this preview. The target is "
                    f"re-checked immediately before execution; if it changed "
                    f"or became unverifiable, the token is invalidated and "
                    f"nothing is sent."
                ),
            )
        try:
            self._gate.consume(
                op, company, _payload_hash(envelope), confirm_token, extra)
        except MutationTokenError as e:
            return self._mutation_refused(
                "Delete", object_type, name, f"REFUSED: {e}",
                xml=envelope, company=company)
        # Re-check immediately before dispatch; invalidate on any change.
        target_id2, _, err2 = validate_fn(company)
        if err2 or target_id2 != target_id:
            return self._mutation_refused(
                "Delete", object_type, name,
                "REFUSED: the delete target changed or became unverifiable "
                "since the preview. Preview again for a fresh token.",
                xml=envelope, company=company)
        return self._post_import(envelope, "Delete", object_type, name, company)

    # -- ledger -----------------------------------------------------------

    def create_ledger(self, name: str, parent: str, preview: bool = True,
                      confirm_token: str = "",
                      _allow_duties_taxes: bool = False,
                      **kwargs) -> ImportResult:
        for f in ("has_opening_balance", "has_credit_limit",
                  "has_tax_percentage", "has_rounding_limit"):
            kwargs.pop(f, None)  # server.py-only flags, already consumed
        # GUARDRAIL: Tally auto-creates its own tax ledgers (CGST/SGST/IGST);
        # never create them manually. To fix one, use alter_ledger. The
        # dedicated create_tax_ledger tool passes _allow_duties_taxes=True
        # (CGST/SGST/IGST names are still refused -- they already exist).
        if (parent and parent.strip().lower() == "duties & taxes"
                and not _allow_duties_taxes):
            return ImportResult(
                success=False, action="Create", object_type="Ledger", name=name,
                preview=preview, xml="",
                message=("REFUSED: parent is 'Duties & Taxes'. Tally "
                         "auto-creates its tax ledgers (CGST/SGST/IGST) -- do "
                         "not create them manually. To fix an existing tax "
                         "ledger, use alter_ledger (tax_type/duty_head/"
                         "tax_percentage params live there)."),
            )
        msg = build_ledger_message(name, "Create", parent=parent, **kwargs)
        res = self.import_masters(msg, "Create", "Ledger", name,
                                  preview=preview, confirm_token=confirm_token)
        self._attach_format_warnings(res, kwargs)
        return res

    def alter_ledger(self, name: str, new_name: Optional[str] = None,
                     preview: bool = True, confirm_token: str = "",
                     **kwargs) -> ImportResult:
        """Alter an existing ledger (rename or update fields).

        Verified pattern: ACTION="Alter" with NAME=name and updated fields.
        Merged 2026-10-06: single definition keeps rename support, strips the
        server.py-only flags, and attaches PAN/GSTIN format warnings.
        """
        for f in ("has_opening_balance", "has_credit_limit",
                  "has_tax_percentage", "has_rounding_limit"):
            kwargs.pop(f, None)  # server.py-only flags, already consumed
        # Rename: Tally ALTER uses NAME attr = old name, <NAME> tag = new name.
        # build_ledger_message sets both from `name`, so handle rename by
        # passing the new name as `name` won't work -- we build the message
        # manually for rename, else use the standard builder.
        if new_name:
            # For rename, construct XML with old NAME attr and new <NAME> tag
            base_xml = build_ledger_message(name, "Alter", **kwargs)
            # Replace the <NAME>old</NAME> tag with new name (first occurrence after opening tag)
            import re as _re
            new_escaped = xml_escape(new_name.strip())
            # The opening tag is <LEDGER NAME="old" ...>, inner <NAME>old</NAME> -> <NAME>new</NAME>
            base_xml = base_xml.replace(f"<NAME>{xml_escape(name.strip())}</NAME>",
                                        f"<NAME>{new_escaped}</NAME>", 1)
            msg = base_xml
        else:
            msg = build_ledger_message(name, "Alter", **kwargs)
        res = self.import_masters(msg, "Alter", "Ledger", name,
                                  preview=preview, confirm_token=confirm_token)
        self._attach_format_warnings(res, kwargs)
        return res

    def delete_ledger(self, name: str, preview: bool = True,
                      confirm_token: str = "") -> ImportResult:
        """Delete a ledger after exact-target validation (fail-closed)."""
        return self._delete_master(
            "Ledger", name,
            lambda company: wrap_import_envelope(build_ledger_message(name, "Delete"), company),
            lambda company: self._validate_exact_master(
                "Ledger", name, company),
            preview=preview, confirm_token=confirm_token)

    def list_ledgers(
        self, company: Optional[str] = None, preview: bool = False
    ) -> dict:
        """List all ledgers with their parent group (read-only)."""
        collection_id = "ListLedger"
        envelope = build_collection_export_envelope(
            collection_id, "Ledger", ["Name", "Parent"], company=company
        )
        if preview:
            return {
                "preview": True,
                "object_type": "Ledger",
                "xml": envelope,
                "message": "PREVIEW only -- nothing sent to Tally.",
            }
        try:
            r = requests.post(
                self.base_url,
                data=envelope.encode("utf-8"),
                headers={"Content-Type": "text/xml"},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {
                "success": False,
                "object_type": "Ledger",
                "error": f"{type(e).__name__}: {e}",
                "message": "Connection to Tally failed. Is Tally Prime running?",
            }
        ledgers = parse_ledger_parents(r.text)
        return {
            "success": True,
            "object_type": "Ledger",
            "count": len(ledgers),
            "ledgers": ledgers,
        }

    # -- group ------------------------------------------------------------

    def create_group(self, name: str, parent: str = "", mode: str = "",
                   nature: Optional[str] = None, alias: Optional[str] = None,
                   sub_ledger: Optional[str] = None, affects_gross_profit: Optional[str] = None,
                   used_for_calculation: Optional[str] = None,
                   allocate_method: Optional[str] = None, is_addable: Optional[str] = None,
                   as_original: Optional[str] = None, affects_stock: Optional[str] = None,
                   preview: bool = True,
                   confirm_token: str = "") -> ImportResult:
        """Two explicit modes (plus auto-inference from parent for back-compat):
        - mode="primary" (alias "autonomous"): standalone top-level group,
          Tally's "Under: Primary". Needs nature. parent must be ""/"Primary".
        - mode="subgroup" (alias "by_default"): group under an existing/default
          group. Needs parent="<existing group name>". nature is ignored.
        - mode="" (default): inferred from parent ("" or "Primary" -> primary).
        """
        p = (parent or "").strip()
        m = (mode or "").strip().lower().replace("-", "_")
        warnings = []
        if m in ("primary", "autonomous", "top_level"):
            top_level = True
            if p and p.lower() != "primary":
                warnings.append(
                    f"parent '{parent}' ignored: mode='{mode}' creates an autonomous "
                    "top-level group (Tally's 'Under: Primary').")
        elif m in ("subgroup", "sub_group", "sub", "under_group", "by_default"):
            top_level = False
        elif m in ("", "auto"):
            top_level = (not p or p.lower() == "primary")
        else:
            return ImportResult(
                success=False, action="Create", object_type="Group", name=name,
                preview=preview,
                message=(f"Unknown mode '{mode}'. Use mode='primary' for an autonomous "
                         "top-level group, or mode='subgroup' with parent='<existing group>'."),
            )
        if top_level:
            key = (nature or "").strip().lower()
            if key not in GROUP_NATURE_FLAGS:
                return ImportResult(
                    success=False, action="Create", object_type="Group", name=name,
                    preview=preview,
                    message=("Top-level group needs nature: 'Assets', 'Liabilities', "
                             "'Income' or 'Expenses' (Tally's 'Nature of Group')."),
                )
        else:
            if not p:
                return ImportResult(
                    success=False, action="Create", object_type="Group", name=name,
                    preview=preview,
                    message=("mode='subgroup' needs parent='<existing group name>' "
                             "(e.g. parent='Sundry Debtors')."),
                )
            if nature:
                warnings.append("nature is ignored for sub-groups (inherited from parent).")
        try:
            msg = build_group_message(
                name, "Create", parent=None if top_level else p, alias=alias,
                sub_ledger=sub_ledger, affects_gross_profit=affects_gross_profit,
                nature=nature, used_for_calculation=used_for_calculation,
                allocate_method=allocate_method, is_addable=is_addable,
                as_original=as_original, affects_stock=affects_stock)
        except ValueError as e:
            return ImportResult(success=False, action="Create", object_type="Group",
                                name=name, preview=preview, message=str(e))
        result = self.import_masters(msg, "Create", "Group", name, preview=preview, confirm_token=confirm_token)
        result.warnings.extend(warnings)
        return result

    def delete_group(self, name: str, preview: bool = True,
                     confirm_token: str = "") -> ImportResult:
        """Delete a group after verifying it exists and is empty."""
        return self._delete_master(
            "Group", name,
            lambda company: wrap_import_envelope(build_group_message(name, "Delete"), company),
            lambda company: self._validate_exact_master("Group", name, company),
            preview=preview, confirm_token=confirm_token)

    def get_group_parents(self) -> Optional[dict]:
        """Read-only: {group name: 'Primary' | parent name} for all groups.

        Returns None if Tally cannot be reached. Used as a pre-check by
        alter_group (existence + current level + target-parent validation).
        """
        envelope = build_collection_export_envelope("ListGroup", "Group", ["PARENT"])
        try:
            r = requests.post(
                self.base_url, data=envelope.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout)
        except requests.RequestException:
            return None
        try:
            pairs = self._parse_group_parents(r.text)
        except (UnsafeXmlError, XmlParseError):
            return None
        result = {}
        for item in pairs:
            gname = item["name"]
            pval = item["parent"]
            result[gname] = "Primary" if ("primary" in pval.lower() or not pval) else pval
        return result

    def alter_group(self, name: str, new_name: Optional[str] = None,
                   parent: Optional[str] = None, nature: Optional[str] = None,
                   alias: Optional[str] = None, sub_ledger: Optional[str] = None,
                   affects_gross_profit: Optional[str] = None,
                   used_for_calculation: Optional[str] = None,
                   is_addable: Optional[str] = None,
                   as_original: Optional[str] = None,
                   affects_stock: Optional[str] = None,
                   preview: bool = True,
                   confirm_token: str = "") -> ImportResult:
        """Alter an existing group. Only provided fields change.

        parent: None = leave unchanged; "Primary" = move to top-level
            (needs nature); "<group>" = move under that group.
        Starts with a read-only pre-check (existence, current level, target
        parent) so mistakes fail fast with clear messages instead of
        half-applied alters.
        """
        if not (new_name or parent is not None or nature or alias or sub_ledger
                or affects_gross_profit or used_for_calculation or is_addable
                or as_original or affects_stock):
            return ImportResult(
                success=False, action="Alter", object_type="Group", name=name,
                preview=preview,
                message="Nothing to alter: pass at least one field to change.")
        parents = self.get_group_parents()
        if parents is None:
            return ImportResult(
                success=False, action="Alter", object_type="Group", name=name,
                preview=preview,
                message="Pre-check failed: could not reach Tally.")
        if name not in parents:
            return ImportResult(
                success=False, action="Alter", object_type="Group", name=name,
                preview=preview,
                message=f"Group '{name}' does not exist.")
        warnings = []
        make_top_level = False
        new_parent = None
        if new_name and new_name.strip() in parents:
            return ImportResult(
                success=False, action="Alter", object_type="Group", name=name,
                preview=preview,
                message=f"Cannot rename: a group named '{new_name.strip()}' already exists.")
        if parent is not None:
            p = parent.strip()
            if p == "" or p.lower() == "primary":
                make_top_level = True
                key = (nature or "").strip().lower()
                if key not in GROUP_NATURE_FLAGS:
                    return ImportResult(
                        success=False, action="Alter", object_type="Group", name=name,
                        preview=preview,
                        message=("Moving to top-level needs nature: 'Assets', 'Liabilities', "
                                 "'Income' or 'Expenses' (Tally's 'Nature of Group')."))
            else:
                if p not in parents:
                    return ImportResult(
                        success=False, action="Alter", object_type="Group", name=name,
                        preview=preview,
                        message=f"Target parent group '{p}' does not exist.")
                if p == name or (new_name and p == new_name.strip()):
                    return ImportResult(
                        success=False, action="Alter", object_type="Group", name=name,
                        preview=preview,
                        message="A group cannot be its own parent.")
                new_parent = p
                if nature:
                    warnings.append(
                        "nature is ignored: the group becomes a sub-group and "
                        "inherits nature from its new parent.")
                    nature = None
        elif nature and parents[name] != "Primary":
            warnings.append(
                "nature is ignored: the group is currently a sub-group "
                "(nature is inherited from its parent).")
            nature = None
        try:
            msg = build_group_alter_message(
                name, new_name=new_name, parent=new_parent,
                make_top_level=make_top_level, nature=nature, alias=alias,
                sub_ledger=sub_ledger, affects_gross_profit=affects_gross_profit,
                used_for_calculation=used_for_calculation,
                is_addable=is_addable, as_original=as_original,
                affects_stock=affects_stock)
        except ValueError as e:
            return ImportResult(success=False, action="Alter", object_type="Group",
                                name=name, preview=preview, message=str(e))
        result = self.import_masters(msg, "Alter", "Group", name, preview=preview, confirm_token=confirm_token)
        result.warnings.extend(warnings)
        return result

    def list_groups(self, parent: str = "") -> dict:
        """Read-only: list accounting groups as {name, parent} items.

        parent="": all groups. parent="Primary": top-level groups only.
        parent="<group>": sub-groups directly under that group.
        """
        parents = self.get_group_parents()
        if parents is None:
            return {"success": False,
                    "message": "Could not reach Tally.",
                    "groups": [], "count": 0}
        items = [{"name": n, "parent": p}
                 for n, p in sorted(parents.items(), key=lambda kv: kv[0].lower())]
        if parent:
            key = parent.strip()
            if key.lower() == "primary":
                items = [i for i in items if i["parent"] == "Primary"]
            else:
                items = [i for i in items if i["parent"] == key]
        return {"success": True, "groups": items, "count": len(items)}

    def create_vouchertype(self, name: str, parent: str, preview: bool = True, confirm_token: str = "") -> ImportResult:
        msg = build_vouchertype_message(name, "Create", parent=parent)
        return self.import_masters(msg, "Create", "VoucherType", name, preview=preview, confirm_token=confirm_token)

    def delete_vouchertype(self, name: str, preview: bool = True,
                     confirm_token: str = "") -> ImportResult:
        """Delete a voucher type after exact-target validation."""
        return self._delete_master(
            "VoucherType", name,
            lambda company: wrap_import_envelope(build_vouchertype_message(name, "Delete"), company),
            lambda company: self._validate_exact_master("VoucherType", name, company),
            preview=preview, confirm_token=confirm_token)

    def alter_vouchertype(self, name: str, new_name: Optional[str] = None,
                          preview: bool = True,
                          confirm_token: str = "") -> ImportResult:
        """Rename/alter a voucher type. Verified pattern 2026-10-05."""
        msg = build_vouchertype_message(name, "Alter", new_name=new_name)
        return self.import_masters(msg, "Alter", "VoucherType", name, preview=preview, confirm_token=confirm_token)

    def list_vouchertypes(self, company: Optional[str] = None) -> dict:
        """List all voucher types (read-only)."""
        envelope = build_collection_export_envelope(
            "ListVoucherType", "VoucherType", ["Name"], company=company
        )
        try:
            r = requests.post(
                self.base_url, data=envelope.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        t = r.content.decode("utf-8", "replace")
        names = re.findall(r"<NAME>([^<]+)</NAME>", t)
        return {"success": True, "count": len(names), "voucher_types": sorted(set(names))}

    # -- stock group ------------------------------------------------------

    def create_stockgroup(
        self,
        name: str,
        hsn_code: Optional[str] = None,
        taxability: Optional[str] = None,
        gst_rate: Optional[float] = None,
        applicable_from: Optional[str] = None,
        preview: bool = True,
        confirm_token: str = "",
    ) -> ImportResult:
        """Create a stock group, then Alter it to add HSN/GST details.

        Two-step because Create with nested HSN/GST lists returned CREATED:0
        in live testing; Create basic + Alter is the verified working pattern.

        Batch safety: ONE confirm_token covers the whole batch. It is bound
        to the exact bytes of both envelopes; on execute both payloads are
        rebuilt and the binding re-verified before the first dispatch.
        """
        try:
            company = get_pinned_company()
        except CompanyNotConfiguredError as e:
            return self._mutation_refused(
                "Create", "StockGroup", name, f"REFUSED: {e}")
        create_msg = build_stockgroup_message(name, "Create")
        alter_msg = None
        if hsn_code or taxability:
            alter_msg = build_stockgroup_message(
                name, "Alter", hsn_code=hsn_code, taxability=taxability,
                gst_rate=gst_rate, applicable_from=applicable_from,
            )
        create_env = wrap_import_envelope(create_msg, company)
        alter_env = (wrap_import_envelope(alter_msg, company)
                     if alter_msg else "")
        combined = create_env + ("\n" + alter_env if alter_env else "")
        op = "Create:StockGroup"
        if preview:
            token, exp = self._gate.issue(
                op, company, _payload_hash(combined),
                f"Create StockGroup '{name}'"
                + (" + HSN/GST alter" if alter_msg else ""))
            return ImportResult(
                success=True, action="Create", object_type="StockGroup",
                name=name, preview=True, xml=combined, company=company,
                confirm_token=token, confirm_token_expires_at=exp,
                message=(
                    "PREVIEW only -- nothing sent to Tally. "
                    f"Would create StockGroup '{name}' in company "
                    f"'{company}'"
                    + (" and then alter it with HSN/GST details"
                       if alter_msg else "")
                    + ". To execute, call again with preview=False and the "
                    "confirm_token from this preview."),
            )
        try:
            self._gate.consume(
                op, company, _payload_hash(combined), confirm_token)
        except MutationTokenError as e:
            return self._mutation_refused(
                "Create", "StockGroup", name, f"REFUSED: {e}",
                xml=combined, company=company)
        res = self._post_import(create_env, "Create", "StockGroup", name,
                                company)
        if not res.success:
            return res
        if alter_msg:
            alter_res = self._post_import(alter_env, "Alter", "StockGroup",
                                          name, company)
            # merge: keep create counts, surface alter result too
            res.message = (
                f"{res.message} | HSN/GST alter: {alter_res.message}"
            )
            res.warnings.extend(alter_res.warnings)
            if not alter_res.success:
                res.success = False
        return res

    def delete_stockgroup(self, name: str, preview: bool = True,
                     confirm_token: str = "") -> ImportResult:
        """Delete a stock group after exact-target validation."""
        return self._delete_master(
            "StockGroup", name,
            lambda company: wrap_import_envelope(build_stockgroup_message(name, "Delete"), company),
            lambda company: self._validate_exact_master("StockGroup", name, company),
            preview=preview, confirm_token=confirm_token)

    def alter_stockgroup(self, name: str, new_name: Optional[str] = None,
                         preview: bool = True,
                         confirm_token: str = "") -> ImportResult:
        """Rename/alter a stock group. Verified pattern 2026-10-05."""
        msg = build_stockgroup_message(name, "Alter", new_name=new_name)
        return self.import_masters(msg, "Alter", "StockGroup", name, preview=preview, confirm_token=confirm_token)

    # -- stock category ---------------------------------------------------

    def create_stockcategory(self, name: str, preview: bool = True, confirm_token: str = "") -> ImportResult:
        msg = build_stockcategory_message(name, "Create")
        return self.import_masters(msg, "Create", "StockCategory", name, preview=preview, confirm_token=confirm_token)

    def alter_stockcategory(self, name: str, new_name: Optional[str] = None,
                            preview: bool = True,
                            confirm_token: str = "") -> ImportResult:
        """Rename/alter a stock category. Verified pattern 2026-10-05."""
        msg = build_stockcategory_message(name, "Alter", new_name=new_name)
        return self.import_masters(msg, "Alter", "StockCategory", name, preview=preview, confirm_token=confirm_token)

    def delete_stockcategory(self, name: str, preview: bool = True,
                     confirm_token: str = "") -> ImportResult:
        """Delete a stock category after exact-target validation."""
        return self._delete_master(
            "StockCategory", name,
            lambda company: wrap_import_envelope(build_stockcategory_message(name, "Delete"), company),
            lambda company: self._validate_exact_master("StockCategory", name, company),
            preview=preview, confirm_token=confirm_token)

    # -- read-only exports ------------------------------------------------

    def export_collection(
        self,
        object_type: str,
        fields: Optional[list] = None,
        company: Optional[str] = None,
        preview: bool = False,
    ) -> dict:
        """Export a Tally master collection (read-only).

        Uses the verified Collection Export envelope (2026-09-28).
        Returns {"names": [...], "count": N, ...}.
        """
        fields = fields or ["Name"]
        collection_id = f"List{object_type}"
        envelope = build_collection_export_envelope(
            collection_id, object_type, fields, company=company
        )
        if preview:
            return {
                "preview": True,
                "object_type": object_type,
                "xml": envelope,
                "message": "PREVIEW only -- nothing sent to Tally.",
            }
        try:
            r = requests.post(
                self.base_url,
                data=envelope.encode("utf-8"),
                headers={"Content-Type": "text/xml"},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {
                "success": False,
                "object_type": object_type,
                "error": f"{type(e).__name__}: {e}",
                "message": "Connection to Tally failed. Is Tally Prime running?",
            }
        names = parse_collection_names(r.text)
        return {
            "success": True,
            "object_type": object_type,
            "count": len(names),
            "names": names,
        }

    def list_stockitems(
        self, company: Optional[str] = None, preview: bool = False
    ) -> dict:
        """List all stock item names in the company (read-only)."""
        return self.export_collection(
            "StockItem", fields=["Name", "Parent"],
            company=company, preview=preview,
        )

    def get_stock_summary(
        self, item_name: str = "", company: Optional[str] = None
    ) -> dict:
        """Stock on hand per item (read-only). Verified 2026-10-03.

        Uses the StockItem collection with ClosingBalance + BaseUnits.
        Returns {"items": [{"name": ..., "quantity": "8.90 Qtls"}, ...]}.
        If item_name is given, returns only that item.
        """
        envelope = build_collection_export_envelope(
            "ListStockItem", "StockItem",
            ["Name", "ClosingBalance", "BaseUnits"], company=company,
        )
        try:
            r = requests.post(
                self.base_url, data=envelope.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        t = r.content.decode("utf-8", "replace")
        items = []
        for m in re.finditer(
                r'<STOCKITEM NAME="([^"]+)"[^>]*>(.*?)</STOCKITEM>', t, re.S):
            name, body = m.group(1), m.group(2)
            if item_name and name.lower() != item_name.strip().lower():
                continue
            cb = re.search(r"<CLOSINGBALANCE[^>]*>([^<]*)</CLOSINGBALANCE>", body)
            qty = cb.group(1).strip() if cb else "0"
            items.append({"name": name, "quantity": qty})
        return {"success": True, "count": len(items), "items": items}

    def get_party_outstanding(
        self, party_name: str = "", company: Optional[str] = None
    ) -> dict:
        """Party-wise outstanding payables/receivables (read-only).

        FIXED 2026-10-05: Now uses get_ledger_report for each party
        to get the TRUE closing balance (includes all voucher types,
        opening balances, matches Tally UI).

        - Dr balance -> debtor (customer owes us)
        - Cr balance -> creditor (we owe supplier)

        Returns {"debtors": [{"party": ..., "amount": ...}, ...],
                 "creditors": [{"party": ..., "amount": ...}, ...]}.
        If party_name is given, returns only that party's balances.
        """
        comp = company or os.environ.get("TALLY_COMPANY", "")
        q_party = party_name.strip().lower() if party_name else ""

        # Step 1: Find all parties from Day Book (Sales/Purchase/Receipt/Payment)
        env = (
            "<ENVELOPE><HEADER><VERSION>1</VERSION>"
            "<TALLYREQUEST>Export</TALLYREQUEST>"
            "<TYPE>Data</TYPE><ID>Day Book</ID></HEADER>"
            "<BODY><DESC><STATICVARIABLES>"
            "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
            f"<SVCURRENTCOMPANY>{xml_escape(comp)}</SVCURRENTCOMPANY>"
            "</STATICVARIABLES></DESC></BODY></ENVELOPE>"
        )
        try:
            r = requests.post(
                self.base_url, data=env.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        t = r.content.decode("utf-8", "replace")

        # Collect unique party names
        parties = set()
        for vm in re.finditer(r"<VOUCHER[^>]*>.*?</VOUCHER>", t, re.S):
            v = vm.group(0)
            # From PARTYLEDGERNAME (Sales/Purchase)
            plm = re.search(r"<PARTYLEDGERNAME>([^<]+)</PARTYLEDGERNAME>", v)
            if plm:
                p = plm.group(1).strip()
                if p and p.lower() not in ("cash",):
                    parties.add(p)
            # From ISPARTYLEDGER (Receipt/Payment)
            for lm in re.finditer(
                    r"<ALLLEDGERENTRIES\.LIST>(.*?)</ALLLEDGERENTRIES\.LIST>", v, re.S):
                body = lm.group(1)
                if "<ISPARTYLEDGER>Yes</ISPARTYLEDGER>" not in body:
                    continue
                lnm = re.search(r"<LEDGERNAME>([^<]+)</LEDGERNAME>", body)
                if lnm:
                    p = lnm.group(1).strip()
                    if p and p.lower() not in ("cash",):
                        parties.add(p)

        # Step 2: Get true closing balance for each party via ledger report
        # Exclude cash/bank ledgers (not parties)
        exclude = {"cash", "pnb bank a/c"}
        debtors = []
        creditors = []
        for party in sorted(parties):
            if party.lower() in exclude:
                continue
            if q_party and party.lower() != q_party:
                continue
            lr = self.get_ledger_report(party, company=comp)
            if not lr.get("success"):
                continue
            bal = lr.get("closing_balance", 0.0)
            btype = lr.get("balance_type", "-")
            if abs(bal) < 0.01:
                continue
            if btype == "Dr":
                debtors.append({"party": party, "amount": round(abs(bal), 2)})
            elif btype == "Cr":
                creditors.append({"party": party, "amount": round(abs(bal), 2)})

        result = {
            "success": True,
            "debtors": debtors,
            "creditors": creditors,
        }
        if party_name:
            result["party"] = party_name.strip()
        return result

    def get_day_book(
        self, date_from: str = "", date_to: str = "",
        voucher_type: str = "", company: Optional[str] = None,
    ) -> dict:
        """Day Book -- all vouchers for a date range (read-only).

        Verified 2026-10-04 via deep research .
        Uses REPORTNAME=Day Book export. Note: Tally's canned Day Book
        may ignore date filters; results are filtered client-side too.

        Returns {"vouchers": [{"date": "20260401", "voucher_type": "Sales",
        "voucher_number": "...", "reference": "...", "party": "...",
        "amount": 9020.0, "narration": "..."}, ...]}.

        Args:
            date_from: YYYYMMDD start (default: books begin).
            date_to: YYYYMMDD end (default: today).
            voucher_type: Filter e.g. "Sales", "Purchase", "Payment".
            company: Company name (defaults to TALLY_COMPANY env var).
        """
        comp = company or os.environ.get("TALLY_COMPANY", "")
        df = yyyymmdd(date_from) if date_from else ""
        dt = yyyymmdd(date_to) if date_to else ""
        env = (
            "<ENVELOPE><HEADER><VERSION>1</VERSION>"
            "<TALLYREQUEST>Export</TALLYREQUEST>"
            "<TYPE>Data</TYPE><ID>Day Book</ID></HEADER>"
            "<BODY><DESC><STATICVARIABLES>"
            "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
            f"<SVCURRENTCOMPANY>{xml_escape(comp)}</SVCURRENTCOMPANY>"
        )
        if df:
            env += f"<SVFROMDATE>{df}</SVFROMDATE>"
        if dt:
            env += f"<SVTODATE>{dt}</SVTODATE>"
        env += "</STATICVARIABLES></DESC></BODY></ENVELOPE>"
        try:
            r = requests.post(
                self.base_url, data=env.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        t = r.content.decode("utf-8", "replace")

        vouchers = []
        for vm in re.finditer(r"<VOUCHER[^>]*>.*?</VOUCHER>", t, re.S):
            v = vm.group(0)
            vtm = re.search(r'VCHTYPE="([^"]+)"', v[:300])
            vtype = vtm.group(1) if vtm else ""
            if voucher_type and vtype.lower() != voucher_type.strip().lower():
                continue
            dm = re.search(r"<DATE>([^<]+)</DATE>", v)
            date = dm.group(1).strip() if dm else ""
            # Client-side date filter (Tally may ignore SVFROMDATE/SVTODATE)
            if df and date < df:
                continue
            if dt and date > dt:
                continue
            vnm = re.search(r"<VOUCHERNUMBER>([^<]*)</VOUCHERNUMBER>", v)
            rfm = re.search(r"<REFERENCE>([^<]*)</REFERENCE>", v)
            plm = re.search(r"<PARTYLEDGERNAME>([^<]*)</PARTYLEDGERNAME>", v)
            nrm = re.search(r"<NARRATION>([^<]*)</NARRATION>", v)
            # Total = sum of positive ledger amounts.
            # Day Book export uses ALLLEDGERENTRIES.LIST for most vouchers,
            # but invoice vouchers may use LEDGERENTRIES.LIST.
            total = 0.0
            found = False
            for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
                tag_total = 0.0
                tag_found = False
                for am in re.finditer(rf"<{tag}>(.*?)</{tag}>", v, re.S):
                    tag_found = True
                    for amt in re.finditer(r"<AMOUNT>([^<]+)</AMOUNT>", am.group(1)):
                        try:
                            a = float(amt.group(1).strip())
                            if a > 0:
                                tag_total += a
                        except ValueError:
                            pass
                if tag_found:
                    total = tag_total
                    break
            vouchers.append({
                "date": date,
                "voucher_type": vtype,
                "voucher_number": vnm.group(1).strip() if vnm else "",
                "reference": rfm.group(1).strip() if rfm else "",
                "party": plm.group(1).strip() if plm else "",
                "amount": round(total, 2),
                "narration": nrm.group(1).strip() if nrm else "",
            })
        vouchers.sort(key=lambda x: (x["date"], x["voucher_number"]))
        return {"success": True, "count": len(vouchers), "vouchers": vouchers}

    def get_ledger_report(
        self, ledger_name: str, date_from: str = "", date_to: str = "",
        company: Optional[str] = None,
    ) -> dict:
        """Ledger Report -- all transactions for one ledger (read-only).

        Verified 2026-10-04 .
        Uses Day Book export + client-side filtering (SVLEDGERNAME is
        unreliable on this Tally build).

        Returns {"ledger": ..., "transactions": [{"date": ..., "voucher_type": ...,
        "voucher_number": ..., "reference": ..., "debit": ..., "credit": ...,
        "narration": ...}], "total_debit": ..., "total_credit": ...,
        "closing_balance": ...}.
        """
        comp = company or os.environ.get("TALLY_COMPANY", "")
        df = yyyymmdd(date_from) if date_from else ""
        dt = yyyymmdd(date_to) if date_to else ""
        # Reuse Day Book export (proven working)
        env = (
            "<ENVELOPE><HEADER><VERSION>1</VERSION>"
            "<TALLYREQUEST>Export</TALLYREQUEST>"
            "<TYPE>Data</TYPE><ID>Day Book</ID></HEADER>"
            "<BODY><DESC><STATICVARIABLES>"
            "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
            f"<SVCURRENTCOMPANY>{xml_escape(comp)}</SVCURRENTCOMPANY>"
            "</STATICVARIABLES></DESC></BODY></ENVELOPE>"
        )
        try:
            r = requests.post(
                self.base_url, data=env.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        t = r.content.decode("utf-8", "replace")

        transactions = []
        total_debit = 0.0
        total_credit = 0.0
        q = ledger_name.strip().lower()
        for vm in re.finditer(r"<VOUCHER[^>]*>.*?</VOUCHER>", t, re.S):
            v = vm.group(0)
            vtm = re.search(r'VCHTYPE="([^"]+)"', v[:300])
            vtype = vtm.group(1) if vtm else ""
            dm = re.search(r"<DATE>([^<]+)</DATE>", v)
            date = dm.group(1).strip() if dm else ""
            if df and date < df:
                continue
            if dt and date > dt:
                continue
            vnm = re.search(r"<VOUCHERNUMBER>([^<]*)</VOUCHERNUMBER>", v)
            rfm = re.search(r"<REFERENCE>([^<]*)</REFERENCE>", v)
            nrm = re.search(r"<NARRATION>([^<]*)</NARRATION>", v)
            debit = 0.0
            credit = 0.0
            for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
                for lm in re.finditer(rf"<{tag}>(.*?)</{tag}>", v, re.S):
                    body = lm.group(1)
                    lnm = re.search(r"<LEDGERNAME>([^<]+)</LEDGERNAME>", body)
                    if not lnm or lnm.group(1).strip().lower() != q:
                        continue
                    amm = re.search(r"<AMOUNT>([^<]+)</AMOUNT>", body)
                    dpm = re.search(r"<ISDEEMEDPOSITIVE>([^<]+)</ISDEEMEDPOSITIVE>", body)
                    if not amm:
                        continue
                    try:
                        a = float(amm.group(1).strip())
                    except ValueError:
                        continue
                    is_debit = dpm and dpm.group(1).strip().lower() == "yes"
                    if is_debit:
                        debit += abs(a)
                    else:
                        credit += abs(a)
            if debit == 0 and credit == 0:
                continue
            total_debit += debit
            total_credit += credit
            transactions.append({
                "date": date,
                "voucher_type": vtype,
                "voucher_number": vnm.group(1).strip() if vnm else "",
                "reference": rfm.group(1).strip() if rfm else "",
                "debit": round(debit, 2),
                "credit": round(credit, 2),
                "narration": nrm.group(1).strip() if nrm else "",
            })
        transactions.sort(key=lambda x: x["date"])
        closing = total_debit - total_credit
        return {
            "success": True,
            "ledger": ledger_name.strip(),
            "count": len(transactions),
            "transactions": transactions,
            "total_debit": round(total_debit, 2),
            "total_credit": round(total_credit, 2),
            "closing_balance": round(closing, 2),
            "balance_type": "Dr" if closing > 0 else "Cr" if closing < 0 else "-",
        }

    def _parse_dsp_report(self, report_name: str, date_from: str = "",
                          date_to: str = "", company: Optional[str] = None) -> dict:
        """Parse DSPACCNAME/DSPACCINFO pair reports (Trial Balance, P&L, BS).

        Verified 2026-10-04 via deep research.
        """
        comp = company or os.environ.get("TALLY_COMPANY", "")
        df = yyyymmdd(date_from) if date_from else ""
        dt = yyyymmdd(date_to) if date_to else ""
        env = (
            "<ENVELOPE><HEADER><VERSION>1</VERSION>"
            "<TALLYREQUEST>Export</TALLYREQUEST>"
            f"<TYPE>Data</TYPE><ID>{report_name}</ID></HEADER>"
            "<BODY><DESC><STATICVARIABLES>"
            "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
            f"<SVCURRENTCOMPANY>{xml_escape(comp)}</SVCURRENTCOMPANY>"
            "<EXPLODEFLAG>Yes</EXPLODEFLAG>"
        )
        if df:
            env += f"<SVFROMDATE>{df}</SVFROMDATE>"
        if dt:
            env += f"<SVTODATE>{dt}</SVTODATE>"
        env += "</STATICVARIABLES></DESC></BODY></ENVELOPE>"
        try:
            r = requests.post(
                self.base_url, data=env.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        t = r.content.decode("utf-8", "replace")
        if "Unknown Request" in t or "Could not find" in t:
            return {"success": False,
                    "error": f"Report '{report_name}' not available."}
        entries = []
        # Trial Balance format: DSPACCNAME + DSPACCINFO pairs
        # P&L format: DSPACCNAME + PLAMT (PLSUBAMT/BSMAINAMT)
        # Balance Sheet format: BSNAME>DSPACCNAME + BSAMT (BSSUBAMT/BSMAINAMT)
        # BSNAME wrapper is optional
        pattern = (r"(?:<BSNAME>\s*)?<DSPACCNAME>\s*<DSPDISPNAME>([^<]*)</DSPDISPNAME>\s*"
                   r"</DSPACCNAME>(?:\s*</BSNAME>)?\s*"
                   r"<(DSPACCINFO|PLAMT|BSAMT)>(.*?)</\2>")
        for m in re.finditer(pattern, t, re.S):
            name = m.group(1).strip()
            tag_type = m.group(2)
            info = m.group(3)
            if not name:
                continue
            dr_amt = 0.0
            cr_amt = 0.0
            if tag_type == "DSPACCINFO":
                # Trial Balance
                dr = re.search(r"<DSPCLDRAMTA[^>]*>([^<]*)</DSPCLDRAMTA>", info)
                if not dr:
                    dr = re.search(r"<DSPCLDRAMT[^>]*>([^<]*)</DSPCLDRAMT>", info)
                cr = re.search(r"<DSPCLCRAMTA[^>]*>([^<]*)</DSPCLCRAMTA>", info)
                if not cr:
                    cr = re.search(r"<DSPCLCRAMT[^>]*>([^<]*)</DSPCLCRAMT>", info)
                if dr and dr.group(1).strip():
                    try:
                        dr_amt = float(dr.group(1).strip())
                    except ValueError:
                        pass
                if cr and cr.group(1).strip():
                    try:
                        cr_amt = float(cr.group(1).strip())
                    except ValueError:
                        pass
            else:
                # P&L (PLAMT) or Balance Sheet (BSAMT)
                # Use BSMAINAMT if present, else PLSUBAMT/BSSUBAMT
                main = re.search(r"<BSMAINAMT[^>]*>([^<]*)</BSMAINAMT>", info)
                sub = re.search(r"<PLSUBAMT[^>]*>([^<]*)</PLSUBAMT>", info)
                if not sub:
                    sub = re.search(r"<BSSUBAMT[^>]*>([^<]*)</BSSUBAMT>", info)
                val = 0.0
                val_str = ""
                if main and main.group(1).strip():
                    val_str = main.group(1).strip()
                elif sub and sub.group(1).strip():
                    val_str = sub.group(1).strip()
                if val_str:
                    try:
                        val = float(val_str)
                    except ValueError:
                        pass
                # Negative = debit side (expense/asset), positive = credit (income/liability)
                if val < 0:
                    dr_amt = abs(val)
                else:
                    cr_amt = val
            if abs(dr_amt) < 0.01 and abs(cr_amt) < 0.01:
                continue
            entries.append({
                "ledger": name,
                "debit": round(dr_amt, 2),
                "credit": round(cr_amt, 2),
            })
        return {"success": True, "count": len(entries), "entries": entries}

    def get_trial_balance(
        self, date_from: str = "", date_to: str = "",
        company: Optional[str] = None,
    ) -> dict:
        """Trial Balance -- all ledgers with closing balances (read-only).

        Verified 2026-10-04 via deep research .
        """
        r = self._parse_dsp_report("Trial Balance", date_from, date_to, company)
        if not r["success"]:
            return r
        total_dr = sum(e["debit"] for e in r["entries"])
        total_cr = sum(e["credit"] for e in r["entries"])
        r["total_debit"] = round(total_dr, 2)
        r["total_credit"] = round(total_cr, 2)
        r["balanced"] = abs(total_dr - total_cr) < 0.01
        return r

    def get_profit_loss(
        self, date_from: str = "", date_to: str = "",
        company: Optional[str] = None,
    ) -> dict:
        """Profit & Loss -- income vs expenses (read-only).

        Verified 2026-10-04 via deep research .
        """
        r = self._parse_dsp_report("Profit and Loss", date_from, date_to, company)
        if not r["success"] or r.get("count", 0) == 0:
            # Try alternate name
            r2 = self._parse_dsp_report("Profit and Loss A/c", date_from, date_to, company)
            if r2["success"] and r2.get("count", 0) > 0:
                r = r2
        return r

    def get_balance_sheet(
        self, as_on: str = "", company: Optional[str] = None,
    ) -> dict:
        """Balance Sheet -- assets vs liabilities as on a date (read-only).

        Verified 2026-10-04 via deep research .
        """
        return self._parse_dsp_report("Balance Sheet", as_on, as_on, company)

    def get_ageing_report(
        self, report_type: str = "receivable", date_from: str = "",
        date_to: str = "", company: Optional[str] = None,
    ) -> dict:
        """Ageing Report -- party outstanding by age buckets (read-only).

        Uses Bills Receivable / Bills Payable. Buckets: 0-30, 30-60,
        60-90, 90+ days. Verified 2026-10-04 via deep research.

        Args:
            report_type: "receivable" (customers) or "payable" (suppliers).
        """
        comp = company or os.environ.get("TALLY_COMPANY", "")
        df = yyyymmdd(date_from) if date_from else ""
        dt = yyyymmdd(date_to) if date_to else ""
        report_id = "Bills Receivable" if report_type == "receivable" else "Bills Payable"
        env = (
            "<ENVELOPE><HEADER><VERSION>1</VERSION>"
            "<TALLYREQUEST>Export</TALLYREQUEST>"
            f"<TYPE>Data</TYPE><ID>{report_id}</ID></HEADER>"
            "<BODY><DESC><STATICVARIABLES>"
            "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
            f"<SVCURRENTCOMPANY>{xml_escape(comp)}</SVCURRENTCOMPANY>"
        )
        if df:
            env += f'<SVFROMDATE TYPE="Date">{df}</SVFROMDATE>'
        if dt:
            env += f'<SVTODATE TYPE="Date">{dt}</SVTODATE>'
        env += "</STATICVARIABLES></DESC></BODY></ENVELOPE>"
        try:
            r = requests.post(
                self.base_url, data=env.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        t = r.content.decode("utf-8", "replace")
        if "Unknown Request" in t:
            return {"success": False, "error": f"Report '{report_id}' not available."}

        from datetime import datetime, date as _date
        today = _date.today()
        bills = []
        # Parse BILLFIXED blocks + following BILLCL/BILLDUE/BILLOVERDUE
        # Date format: "1-Apr-26" (not YYYYMMDD)
        month_map = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
                     "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
        pattern = (r"<BILLFIXED>(.*?)</BILLFIXED>\s*"
                   r"<BILLCL>([^<]*)</BILLCL>\s*"
                   r"<BILLDUE>([^<]*)</BILLDUE>")
        for m in re.finditer(pattern, t, re.S):
            body = m.group(1)
            bcl_str = m.group(2).strip()
            bdate = re.search(r"<BILLDATE[^>]*>([^<]*)</BILLDATE>", body)
            bref = re.search(r"<BILLREF>([^<]*)</BILLREF>", body)
            bparty = re.search(r"<BILLPARTY>([^<]*)</BILLPARTY>", body)
            bill_date_str = bdate.group(1).strip() if bdate else ""
            amount = 0.0
            if bcl_str:
                try:
                    amount = abs(float(bcl_str))
                except ValueError:
                    pass
            if amount < 0.01:
                continue
            # Age in days - handle "1-Apr-26" format
            age_days = 0
            try:
                parts = bill_date_str.replace("-", " ").split()
                if len(parts) == 3:
                    day = int(parts[0])
                    month = month_map.get(parts[1], 1)
                    year = int(parts[2])
                    if year < 100:
                        year += 2000
                    bd = _date(year, month, day)
                    age_days = (today - bd).days
            except (ValueError, IndexError):
                pass
            if age_days <= 30:
                bucket = "0-30"
            elif age_days <= 60:
                bucket = "30-60"
            elif age_days <= 90:
                bucket = "60-90"
            else:
                bucket = "90+"
            bills.append({
                "party": bparty.group(1).strip() if bparty else "",
                "bill_ref": bref.group(1).strip() if bref else "",
                "bill_date": bill_date_str,
                "amount": round(amount, 2),
                "age_days": age_days,
                "bucket": bucket,
            })
        # Summarize by bucket
        buckets = {"0-30": 0.0, "30-60": 0.0, "60-90": 0.0, "90+": 0.0}
        for b in bills:
            buckets[b["bucket"]] += b["amount"]
        return {
            "success": True,
            "type": report_type,
            "count": len(bills),
            "bills": bills,
            "buckets": {k: round(v, 2) for k, v in buckets.items()},
            "total": round(sum(buckets.values()), 2),
        }

    def get_godown_summary(
        self, godown_name: str = "", item_name: str = "",
        company: Optional[str] = None,
    ) -> dict:
        """Godown Summary -- stock by godown/location (read-only).

        Reconstructs godown-wise stock from voucher inventory entries
        (Tally has no direct Godown Summary XML report).
        Verified 2026-10-04 .

        Args:
            godown_name: Filter by godown (e.g. "Main Location"). Empty = all.
            item_name: Filter by stock item. Empty = all.
            company: Company name (defaults to TALLY_COMPANY env var).

        Returns {"godowns": {godown: {item: qty}}, "total_items": ...}.
        """
        comp = company or os.environ.get("TALLY_COMPANY", "")
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
            "<FETCH>AllInventoryEntries.STOCKITEMNAME</FETCH>"
            "<FETCH>AllInventoryEntries.BATCHALLOCATIONS.GODOWNNAME</FETCH>"
            "<FETCH>AllInventoryEntries.BATCHALLOCATIONS.ACTUALQTY</FETCH>"
            "</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
        )
        try:
            r = requests.post(
                self.base_url, data=env.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        t = r.content.decode("utf-8", "replace")

        godowns = {}  # godown -> {item -> qty}
        q_godown = godown_name.strip().lower() if godown_name else ""
        q_item = item_name.strip().lower() if item_name else ""

        # Parse each inventory entry (tag is ALLINVENTORYENTRIES.LIST)
        for im in re.finditer(
            r"<ALLINVENTORYENTRIES\.LIST>(.*?)</ALLINVENTORYENTRIES\.LIST>", t, re.S
        ):
            entry = im.group(1)
            snm = re.search(r"<STOCKITEMNAME[^>]*>([^<]+)</STOCKITEMNAME>", entry)
            if not snm:
                continue
            item = snm.group(1).strip()
            if q_item and item.lower() != q_item:
                continue
            # Each batch allocation has godown + qty
            for bm in re.finditer(
                r"<BATCHALLOCATIONS\.LIST>(.*?)</BATCHALLOCATIONS\.LIST>", entry, re.S
            ):
                bbody = bm.group(1)
                gnm = re.search(
                    r'<GODOWNNAME[^>]*>([^<]+)</GODOWNNAME>', bbody)
                aqm = re.search(
                    r'<ACTUALQTY[^>]*>([^<]+)</ACTUALQTY>', bbody)
                if not gnm or not aqm:
                    continue
                godown = gnm.group(1).strip()
                # Clean godown name (remove &#4; prefix if present)
                godown = re.sub(r"^&#4;\s*", "", godown)
                if q_godown and godown.lower() != q_godown:
                    continue
                qty_str = aqm.group(1).strip()
                # Parse " 9.90 Qtls" -> 9.90
                qm = re.match(r"([-\d.]+)", qty_str)
                if not qm:
                    continue
                try:
                    qty = float(qm.group(1))
                except ValueError:
                    continue
                if godown not in godowns:
                    godowns[godown] = {}
                godowns[godown][item] = godowns[godown].get(item, 0.0) + qty

        # Round and filter zero
        result = {}
        for g, items in godowns.items():
            r_items = {k: round(v, 2) for k, v in items.items() if abs(v) >= 0.005}
            if r_items:
                result[g] = r_items
        return {
            "success": True,
            "godowns": result,
            "godown_count": len(result),
        }

    def get_gstr1(
        self, date_from: str = "", date_to: str = "",
        company: Optional[str] = None,
    ) -> dict:
        """GSTR-1 -- GST sales return data (read-only).

        Reconstructs GSTR-1 tables from Sales/Credit Note vouchers
        (Tally has no direct GSTR-1 XML report).
        Verified 2026-10-05 via deep research.

        Tables: B2B (registered dealers), B2CL (big interstate B2C),
        B2CS (small B2C), CDNR (credit notes).

        Returns {"b2b": [...], "b2cl": [...], "b2cs": [...], "cdnr": [...],
        "summary": {"total_invoices": ..., "total_taxable": ..., "total_tax": ...}}.
        """
        comp = company or os.environ.get("TALLY_COMPANY", "")
        df = yyyymmdd(date_from) if date_from else ""
        dt = yyyymmdd(date_to) if date_to else ""

        # Fetch sales-type vouchers with GST fields
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
            "<FETCH>DATE, VOUCHERTYPENAME, VOUCHERNUMBER, REFERENCE</FETCH>"
            "<FETCH>PARTYLEDGERNAME, PARTYGSTIN, PLACEOFSUPPLY</FETCH>"
            "<FETCH>AllLedgerEntries.LEDGERNAME</FETCH>"
            "<FETCH>AllLedgerEntries.AMOUNT</FETCH>"
            "<FETCH>AllLedgerEntries.ISDEEMEDPOSITIVE</FETCH>"
            "</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
        )
        try:
            r = requests.post(
                self.base_url, data=env.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        t = r.content.decode("utf-8", "replace")

        b2b = []
        b2cl = []
        b2cs_agg = {}  # (pos, rate) -> {txval, cgst, sgst, igst}
        cdnr = []
        total_taxable = 0.0
        total_tax = 0.0
        invoice_count = 0

        for vm in re.finditer(r"<VOUCHER[\s>].*?</VOUCHER>", t, re.S):
            v = vm.group(0)
            vtm = re.search(r'VCHTYPE="([^"]+)"', v[:500])
            if not vtm:
                # Fallback to VOUCHERTYPENAME
                vtn = re.search(r"<VOUCHERTYPENAME[^>]*>([^<]+)</VOUCHERTYPENAME>", v)
                vtype = vtn.group(1).strip() if vtn else ""
            else:
                vtype = vtm.group(1)
            # Only Sales and Credit Note (sales returns)
            if vtype not in ("Sales", "Credit Note"):
                continue

            dm = re.search(r"<DATE[^>]*>([^<]+)</DATE>", v)
            date = dm.group(1).strip() if dm else ""
            if df and date < df:
                continue
            if dt and date > dt:
                continue

            vnm = re.search(r"<VOUCHERNUMBER[^>]*>([^<]*)</VOUCHERNUMBER>", v)
            inv_num = vnm.group(1).strip() if vnm else ""
            plm = re.search(r"<PARTYLEDGERNAME[^>]*>([^<]+)</PARTYLEDGERNAME>", v)
            party = plm.group(1).strip() if plm else ""
            # Skip cash sales (B2C without invoice tracking)
            if party.lower() == "cash":
                continue

            pgm = re.search(r"<PARTYGSTIN[^>]*>([^<]*)</PARTYGSTIN>", v)
            gstin = pgm.group(1).strip() if pgm else ""
            # Clean &#4; prefix
            gstin = re.sub(r"^&#4;\s*", "", gstin)
            if gstin.lower() in ("", "not applicable", "unregistered"):
                gstin = ""

            psm = re.search(r"<PLACEOFSUPPLY[^>]*>([^<]*)</PLACEOFSUPPLY>", v)
            pos = psm.group(1).strip() if psm else ""
            pos = re.sub(r"^&#4;\s*", "", pos)

            # Parse ledger entries for taxable value and tax
            taxable = 0.0
            cgst = 0.0
            sgst = 0.0
            igst = 0.0
            for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
                for lm in re.finditer(rf"<{tag}>(.*?)</{tag}>", v, re.S):
                    body = lm.group(1)
                    lnm = re.search(r"<LEDGERNAME[^>]*>([^<]+)</LEDGERNAME>", body)
                    amm = re.search(r"<AMOUNT[^>]*>([^<]+)</AMOUNT>", body)
                    if not lnm or not amm:
                        continue
                    lname = lnm.group(1).strip()
                    try:
                        amt = abs(float(amm.group(1).strip()))
                    except ValueError:
                        continue
                    lname_lower = lname.lower()
                    if "cgst" in lname_lower:
                        cgst += amt
                    elif "sgst" in lname_lower:
                        sgst += amt
                    elif "igst" in lname_lower:
                        igst += amt
                    elif lname == party:
                        # Party ledger = invoice total
                        pass
                    elif "sales" in lname_lower and "tax" not in lname_lower:
                        taxable += amt
                    # Skip round-off, discount etc for simplicity

            # If no separate sales ledger found, derive from party amount minus tax
            if taxable < 0.01:
                # Find party amount
                for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
                    for lm in re.finditer(rf"<{tag}>(.*?)</{tag}>", v, re.S):
                        body = lm.group(1)
                        lnm = re.search(r"<LEDGERNAME[^>]*>([^<]+)</LEDGERNAME>", body)
                        amm = re.search(r"<AMOUNT[^>]*>([^<]+)</AMOUNT>", body)
                        if lnm and amm and lnm.group(1).strip() == party:
                            try:
                                party_amt = abs(float(amm.group(1).strip()))
                                taxable = party_amt - cgst - sgst - igst
                            except ValueError:
                                pass
                            break

            tax_total = cgst + sgst + igst
            inv_value = taxable + tax_total

            # Format date as DD-MM-YYYY
            date_fmt = date
            if len(date) == 8 and date.isdigit():
                date_fmt = f"{date[6:8]}-{date[4:6]}-{date[0:4]}"

            is_credit_note = (vtype == "Credit Note")

            if is_credit_note:
                # CDNR: sales return
                cdnr.append({
                    "party": party,
                    "gstin": gstin,
                    "note_number": inv_num,
                    "note_date": date_fmt,
                    "invoice_value": round(inv_value, 2),
                    "taxable_value": round(taxable, 2),
                    "cgst": round(cgst, 2),
                    "sgst": round(sgst, 2),
                    "igst": round(igst, 2),
                    "place_of_supply": pos,
                })
            elif gstin:
                # B2B: registered dealer
                b2b.append({
                    "party": party,
                    "gstin": gstin,
                    "invoice_number": inv_num,
                    "invoice_date": date_fmt,
                    "invoice_value": round(inv_value, 2),
                    "taxable_value": round(taxable, 2),
                    "cgst": round(cgst, 2),
                    "sgst": round(sgst, 2),
                    "igst": round(igst, 2),
                    "place_of_supply": pos,
                })
            elif inv_value > 100000:
                # B2CL: big interstate B2C (simplified: >1L without GSTIN)
                b2cl.append({
                    "party": party,
                    "invoice_number": inv_num,
                    "invoice_date": date_fmt,
                    "invoice_value": round(inv_value, 2),
                    "taxable_value": round(taxable, 2),
                    "cgst": round(cgst, 2),
                    "sgst": round(sgst, 2),
                    "igst": round(igst, 2),
                    "place_of_supply": pos,
                })
            else:
                # B2CS: aggregate small B2C by place of supply
                key = pos or "Unknown"
                if key not in b2cs_agg:
                    b2cs_agg[key] = {"txval": 0, "cgst": 0, "sgst": 0, "igst": 0, "count": 0}
                b2cs_agg[key]["txval"] += taxable
                b2cs_agg[key]["cgst"] += cgst
                b2cs_agg[key]["sgst"] += sgst
                b2cs_agg[key]["igst"] += igst
                b2cs_agg[key]["count"] += 1

            if not is_credit_note:
                total_taxable += taxable
                total_tax += tax_total
                invoice_count += 1

        b2cs = [
            {
                "place_of_supply": pos,
                "invoice_count": v["count"],
                "taxable_value": round(v["txval"], 2),
                "cgst": round(v["cgst"], 2),
                "sgst": round(v["sgst"], 2),
                "igst": round(v["igst"], 2),
            }
            for pos, v in sorted(b2cs_agg.items())
        ]

        return {
            "success": True,
            "period": {"from": date_from or "books_begin", "to": date_to or "today"},
            "b2b": b2b,
            "b2cl": b2cl,
            "b2cs": b2cs,
            "cdnr": cdnr,
            "summary": {
                "b2b_count": len(b2b),
                "b2cl_count": len(b2cl),
                "b2cs_count": len(b2cs),
                "cdnr_count": len(cdnr),
                "total_invoices": invoice_count,
                "total_taxable": round(total_taxable, 2),
                "total_tax": round(total_tax, 2),
            },
        }

    def get_gstr3b(
        self, date_from: str = "", date_to: str = "",
        company: Optional[str] = None,
    ) -> dict:
        """GSTR-3B -- GST summary return data (read-only).

        Reconstructs GSTR-3B Table 3.1 (outward) and Table 4 (ITC)
        from Sales/Purchase vouchers.
        Verified 2026-10-05 via deep research.

        Returns {"table_3_1": {...}, "table_4": {...}, "summary": {...}}.
        """
        comp = company or os.environ.get("TALLY_COMPANY", "")
        df = yyyymmdd(date_from) if date_from else ""
        dt = yyyymmdd(date_to) if date_to else ""

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
            "<FETCH>DATE, VOUCHERTYPENAME, VOUCHERNUMBER</FETCH>"
            "<FETCH>PARTYLEDGERNAME</FETCH>"
            "<FETCH>AllLedgerEntries.LEDGERNAME</FETCH>"
            "<FETCH>AllLedgerEntries.AMOUNT</FETCH>"
            "</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
        )
        try:
            r = requests.post(
                self.base_url, data=env.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        t = r.content.decode("utf-8", "replace")

        # Table 3.1: Outward supplies (from Sales)
        t31_taxable = 0.0
        t31_cgst = 0.0
        t31_sgst = 0.0
        t31_igst = 0.0

        # Table 4: ITC (from Purchases)
        t4_taxable = 0.0
        t4_cgst = 0.0
        t4_sgst = 0.0
        t4_igst = 0.0

        for vm in re.finditer(r"<VOUCHER[\s>].*?</VOUCHER>", t, re.S):
            v = vm.group(0)
            vtm = re.search(r'VCHTYPE="([^"]+)"', v[:500])
            if not vtm:
                vtn = re.search(r"<VOUCHERTYPENAME[^>]*>([^<]+)</VOUCHERTYPENAME>", v)
                vtype = vtn.group(1).strip() if vtn else ""
            else:
                vtype = vtm.group(1)

            dm = re.search(r"<DATE[^>]*>([^<]+)</DATE>", v)
            date = dm.group(1).strip() if dm else ""
            if df and date < df:
                continue
            if dt and date > dt:
                continue

            # Skip cash sales for 3B (they're B2C, included in aggregate)
            plm = re.search(r"<PARTYLEDGERNAME[^>]*>([^<]+)</PARTYLEDGERNAME>", v)
            party = plm.group(1).strip() if plm else ""

            taxable = 0.0
            cgst = 0.0
            sgst = 0.0
            igst = 0.0

            for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
                for lm in re.finditer(rf"<{tag}>(.*?)</{tag}>", v, re.S):
                    body = lm.group(1)
                    lnm = re.search(r"<LEDGERNAME[^>]*>([^<]+)</LEDGERNAME>", body)
                    amm = re.search(r"<AMOUNT[^>]*>([^<]+)</AMOUNT>", body)
                    if not lnm or not amm:
                        continue
                    lname = lnm.group(1).strip()
                    try:
                        amt = abs(float(amm.group(1).strip()))
                    except ValueError:
                        continue
                    ll = lname.lower()
                    if "cgst" in ll:
                        cgst += amt
                    elif "sgst" in ll or "utgst" in ll:
                        sgst += amt
                    elif "igst" in ll:
                        igst += amt
                    elif "sales" in ll and "tax" not in ll:
                        taxable += amt
                    elif "purchase" in ll and "tax" not in ll:
                        taxable += amt

            # Derive taxable from party amount if not found
            if taxable < 0.01 and party:
                for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
                    for lm in re.finditer(rf"<{tag}>(.*?)</{tag}>", v, re.S):
                        body = lm.group(1)
                        lnm = re.search(r"<LEDGERNAME[^>]*>([^<]+)</LEDGERNAME>", body)
                        amm = re.search(r"<AMOUNT[^>]*>([^<]+)</AMOUNT>", body)
                        if lnm and amm and lnm.group(1).strip() == party:
                            try:
                                taxable = abs(float(amm.group(1).strip())) - cgst - sgst - igst
                            except ValueError:
                                pass
                            break

            if vtype == "Sales" and party.lower() != "cash":
                t31_taxable += taxable
                t31_cgst += cgst
                t31_sgst += sgst
                t31_igst += igst
            elif vtype == "Purchase":
                t4_taxable += taxable
                t4_cgst += cgst
                t4_sgst += sgst
                t4_igst += igst
            elif vtype == "Credit Note":
                # Sales return reduces outward
                t31_taxable -= taxable
                t31_cgst -= cgst
                t31_sgst -= sgst
                t31_igst -= igst
            elif vtype == "Debit Note":
                # Purchase return reduces ITC
                t4_taxable -= taxable
                t4_cgst -= cgst
                t4_sgst -= sgst
                t4_igst -= igst

        return {
            "success": True,
            "period": {"from": date_from or "books_begin", "to": date_to or "today"},
            "table_3_1": {
                "description": "Outward taxable supplies",
                "taxable_value": round(t31_taxable, 2),
                "igst": round(t31_igst, 2),
                "cgst": round(t31_cgst, 2),
                "sgst": round(t31_sgst, 2),
                "total_tax": round(t31_cgst + t31_sgst + t31_igst, 2),
            },
            "table_4": {
                "description": "Eligible ITC",
                "taxable_value": round(t4_taxable, 2),
                "igst": round(t4_igst, 2),
                "cgst": round(t4_cgst, 2),
                "sgst": round(t4_sgst, 2),
                "total_itc": round(t4_cgst + t4_sgst + t4_igst, 2),
            },
            "summary": {
                "net_tax_payable": round(
                    (t31_cgst + t31_sgst + t31_igst) - (t4_cgst + t4_sgst + t4_igst), 2
                ),
            },
        }

    def get_sales_register(
        self, date_from: str = "", date_to: str = "",
        company: Optional[str] = None,
    ) -> dict:
        """Sales Register -- all sales vouchers with totals (read-only).

        Uses Day Book filtered by Sales voucher type.
        Verified 2026-10-05 .
        """
        r = self.get_day_book(date_from, date_to, "Sales", company)
        if not r.get("success"):
            return r
        vouchers = r.get("vouchers", [])
        total = sum(v.get("amount", 0) for v in vouchers)
        return {
            "success": True,
            "count": len(vouchers),
            "vouchers": vouchers,
            "total_sales": round(total, 2),
        }

    def get_purchase_register(
        self, date_from: str = "", date_to: str = "",
        company: Optional[str] = None,
    ) -> dict:
        """Purchase Register -- all purchase vouchers with totals (read-only).

        Uses Day Book filtered by Purchase voucher type.
        Verified 2026-10-05 .
        """
        r = self.get_day_book(date_from, date_to, "Purchase", company)
        if not r.get("success"):
            return r
        vouchers = r.get("vouchers", [])
        total = sum(v.get("amount", 0) for v in vouchers)
        return {
            "success": True,
            "count": len(vouchers),
            "vouchers": vouchers,
            "total_purchase": round(total, 2),
        }

    def get_payment_register(
        self, date_from: str = "", date_to: str = "",
        company: Optional[str] = None,
    ) -> dict:
        """Payment Register -- all payment vouchers with totals (read-only).

        Verified 2026-10-05 .
        """
        r = self.get_day_book(date_from, date_to, "Payment", company)
        if not r.get("success"):
            return r
        vouchers = r.get("vouchers", [])
        total = sum(v.get("amount", 0) for v in vouchers)
        return {
            "success": True,
            "count": len(vouchers),
            "vouchers": vouchers,
            "total_payment": round(total, 2),
        }

    def get_receipt_register(
        self, date_from: str = "", date_to: str = "",
        company: Optional[str] = None,
    ) -> dict:
        """Receipt Register -- all receipt vouchers with totals (read-only).

        Verified 2026-10-05 .
        """
        r = self.get_day_book(date_from, date_to, "Receipt", company)
        if not r.get("success"):
            return r
        vouchers = r.get("vouchers", [])
        total = sum(v.get("amount", 0) for v in vouchers)
        return {
            "success": True,
            "count": len(vouchers),
            "vouchers": vouchers,
            "total_receipt": round(total, 2),
        }

    def get_cash_flow(
        self, date_from: str = "", date_to: str = "",
        company: Optional[str] = None,
    ) -> dict:
        """Cash Flow -- cash movement statement (read-only).

        Uses REPORTNAME=Cash Flow. Shows Operating/Investing/Financing
        activities. Verified 2026-10-05 via deep research.
        """
        comp = company or os.environ.get("TALLY_COMPANY", "")
        df = yyyymmdd(date_from) if date_from else ""
        dt = yyyymmdd(date_to) if date_to else ""
        env = (
            "<ENVELOPE><HEADER><VERSION>1</VERSION>"
            "<TALLYREQUEST>Export</TALLYREQUEST>"
            "<TYPE>Data</TYPE><ID>Cash Flow</ID></HEADER>"
            "<BODY><DESC><STATICVARIABLES>"
            "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
            f"<SVCURRENTCOMPANY>{xml_escape(comp)}</SVCURRENTCOMPANY>"
        )
        if df:
            env += f"<SVFROMDATE>{df}</SVFROMDATE>"
        if dt:
            env += f"<SVTODATE>{dt}</SVTODATE>"
        env += "</STATICVARIABLES></DESC></BODY></ENVELOPE>"
        try:
            r = requests.post(
                self.base_url, data=env.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        t = r.content.decode("utf-8", "replace")
        if "Unknown Request" in t or "Could not find" in t:
            return {"success": False, "error": "Cash Flow report not available."}

        # Parse Cash Flow (DSPPERIOD + DSPACCINFO with DSPDRAMT/DSPCRAMT/DSPCLAMT)
        entries = []
        pattern = (r"<DSPPERIOD>([^<]*)</DSPPERIOD>\s*"
                   r"<DSPACCINFO>(.*?)</DSPACCINFO>")
        for m in re.finditer(pattern, t, re.S):
            period = m.group(1).strip()
            info = m.group(2)
            if not period:
                continue
            dr = 0.0
            cr = 0.0
            cl = 0.0
            drm = re.search(r"<DSPDRAMTA[^>]*>([^<]*)</DSPDRAMTA>", info)
            crm = re.search(r"<DSPCRAMTA[^>]*>([^<]*)</DSPCRAMTA>", info)
            clm = re.search(r"<DSPCLAMTA[^>]*>([^<]*)</DSPCLAMTA>", info)
            if drm and drm.group(1).strip():
                try:
                    dr = float(drm.group(1).strip())
                except ValueError:
                    pass
            if crm and crm.group(1).strip():
                try:
                    cr = float(crm.group(1).strip())
                except ValueError:
                    pass
            if clm and clm.group(1).strip():
                try:
                    cl = float(clm.group(1).strip())
                except ValueError:
                    pass
            entries.append({
                "period": period,
                "debit": round(dr, 2),
                "credit": round(cr, 2),
                "closing": round(cl, 2),
            })

        return {"success": True, "count": len(entries), "entries": entries}

    def get_bank_reconciliation(
        self, bank_ledger: str, date_from: str = "", date_to: str = "",
        company: Optional[str] = None,
    ) -> dict:
        """Bank Reconciliation -- unreconciled bank entries (read-only).

        Finds bank vouchers without BANKDATE (unreconciled).
        Verified 2026-10-05 via deep research.

        Args:
            bank_ledger: Bank ledger name (e.g. "PNB Bank A/c").
            date_from: Start date YYYYMMDD.
            date_to: End date YYYYMMDD.
        """
        comp = company or os.environ.get("TALLY_COMPANY", "")
        df = yyyymmdd(date_from) if date_from else ""
        dt = yyyymmdd(date_to) if date_to else ""

        # Get bank ledger report
        lr = self.get_ledger_report(bank_ledger, date_from, date_to, comp)
        if not lr.get("success"):
            return lr

        # For BRS, we need BANKDATE per voucher - fetch via collection
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
            "<FETCH>DATE, VOUCHERTYPENAME, VOUCHERNUMBER, REFERENCE</FETCH>"
            "<FETCH>AllLedgerEntries.LEDGERNAME</FETCH>"
            "<FETCH>AllLedgerEntries.AMOUNT</FETCH>"
            "<FETCH>AllLedgerEntries.BANKDATE</FETCH>"
            "</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
        )
        try:
            r = requests.post(
                self.base_url, data=env.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        t = r.content.decode("utf-8", "replace")

        q_bank = bank_ledger.strip().lower()
        reconciled = []
        unreconciled = []

        for vm in re.finditer(r"<VOUCHER[\s>].*?</VOUCHER>", t, re.S):
            v = vm.group(0)
            dm = re.search(r"<DATE[^>]*>([^<]+)</DATE>", v)
            date = dm.group(1).strip() if dm else ""
            if df and date < df:
                continue
            if dt and date > dt:
                continue

            vtm = re.search(r'VCHTYPE="([^"]+)"', v[:500])
            vtype = vtm.group(1) if vtm else ""
            vnm = re.search(r"<VOUCHERNUMBER[^>]*>([^<]*)</VOUCHERNUMBER>", v)
            vnum = vnm.group(1).strip() if vnm else ""

            # Check if this voucher touches the bank ledger
            has_bank = False
            bank_amt = 0.0
            bank_date = ""
            for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
                for lm in re.finditer(rf"<{tag}>(.*?)</{tag}>", v, re.S):
                    body = lm.group(1)
                    lnm = re.search(r"<LEDGERNAME[^>]*>([^<]+)</LEDGERNAME>", body)
                    if not lnm or lnm.group(1).strip().lower() != q_bank:
                        continue
                    has_bank = True
                    amm = re.search(r"<AMOUNT[^>]*>([^<]+)</AMOUNT>", body)
                    if amm:
                        try:
                            bank_amt = float(amm.group(1).strip())
                        except ValueError:
                            pass
                    bdm = re.search(r"<BANKDATE[^>]*>([^<]*)</BANKDATE>", body)
                    if bdm:
                        bank_date = bdm.group(1).strip()

            if not has_bank:
                continue

            entry = {
                "date": date,
                "voucher_type": vtype,
                "voucher_number": vnum,
                "amount": round(abs(bank_amt), 2),
                "dr_cr": "Dr" if bank_amt > 0 else "Cr",
                "bank_date": bank_date,
            }
            if bank_date:
                reconciled.append(entry)
            else:
                unreconciled.append(entry)

        return {
            "success": True,
            "bank_ledger": bank_ledger,
            "reconciled_count": len(reconciled),
            "unreconciled_count": len(unreconciled),
            "reconciled": reconciled,
            "unreconciled": unreconciled,
        }

    def get_gstr9(
        self, date_from: str = "", date_to: str = "",
        company: Optional[str] = None,
    ) -> dict:
        """GSTR-9 -- Annual GST return summary (read-only).

        Aggregates GSTR-1 (outward) and GSTR-3B (ITC) data for the year.
        V1: Table 4 (outward), Table 6-8 (ITC), Table 9 (tax paid).
        Verified 2026-10-05 via deep research.
        """
        # Get GSTR-1 data (outward supplies)
        g1 = self.get_gstr1(date_from, date_to, company)
        # Get GSTR-3B data (summary)
        g3 = self.get_gstr3b(date_from, date_to, company)

        if not g1.get("success"):
            return {"success": False, "error": "GSTR-1 failed: " + g1.get("error", "")}
        if not g3.get("success"):
            return {"success": False, "error": "GSTR-3B failed: " + g3.get("error", "")}

        g1_summary = g1.get("summary", {})
        t31 = g3.get("table_3_1", {})
        t4 = g3.get("table_4", {})

        return {
            "success": True,
            "period": {"from": date_from or "books_begin", "to": date_to or "today"},
            "table_4": {
                "description": "Outward supplies (from GSTR-1)",
                "b2b_count": g1_summary.get("b2b_count", 0),
                "b2cl_count": g1_summary.get("b2cl_count", 0),
                "b2cs_count": g1_summary.get("b2cs_count", 0),
                "taxable_value": t31.get("taxable_value", 0),
                "cgst": t31.get("cgst", 0),
                "sgst": t31.get("sgst", 0),
                "igst": t31.get("igst", 0),
            },
            "table_6_7_8": {
                "description": "ITC availed (from GSTR-3B Table 4)",
                "taxable_value": t4.get("taxable_value", 0),
                "cgst": t4.get("cgst", 0),
                "sgst": t4.get("sgst", 0),
                "igst": t4.get("igst", 0),
                "total_itc": t4.get("total_itc", 0),
            },
            "table_9": {
                "description": "Tax paid",
                "net_tax_payable": g3.get("summary", {}).get("net_tax_payable", 0),
            },
        }

    # -- stock item -------------------------------------------------------

    def create_stockitem(self, name: str, unit: str, preview: bool = True,
                         confirm_token: str = "",
                         group: Optional[str] = None,
                         category: Optional[str] = None) -> ImportResult:
        msg = build_stockitem_message(name, "Create", unit=unit,
                                      group=group, category=category)
        return self.import_masters(msg, "Create", "StockItem", name,
                                   preview=preview, confirm_token=confirm_token)

    def alter_stockitem(self, name: str, preview: bool = True,
                        confirm_token: str = "",
                        group: Optional[str] = None,
                        category: Optional[str] = None) -> ImportResult:
        msg = build_stockitem_message(name, "Alter",
                                      group=group, category=category)
        return self.import_masters(msg, "Alter", "StockItem", name,
                                   preview=preview, confirm_token=confirm_token)

    def delete_stockitem(self, name: str, preview: bool = True,
                     confirm_token: str = "") -> ImportResult:
        """Delete a stock item after exact-target validation."""
        return self._delete_master(
            "StockItem", name,
            lambda company: wrap_import_envelope(build_stockitem_message(name, "Delete"), company),
            lambda company: self._validate_exact_master("StockItem", name, company),
            preview=preview, confirm_token=confirm_token)

    # -- godown -----------------------------------------------------------

    def create_godown(self, name: str, preview: bool = True,
                      confirm_token: str = "",
                      parent: Optional[str] = None) -> ImportResult:
        msg = build_godown_message(name, "Create", parent=parent)
        return self.import_masters(msg, "Create", "Godown", name,
                                   preview=preview, confirm_token=confirm_token)

    def delete_godown(self, name: str, preview: bool = True,
                     confirm_token: str = "") -> ImportResult:
        """Delete a godown after exact-target validation."""
        return self._delete_master(
            "Godown", name,
            lambda company: wrap_import_envelope(build_godown_message(name, "Delete"), company),
            lambda company: self._validate_exact_master("Godown", name, company),
            preview=preview, confirm_token=confirm_token)

    def alter_godown(self, name: str, new_name: Optional[str] = None,
                     preview: bool = True,
                     confirm_token: str = "") -> ImportResult:
        """Rename/alter a godown. Verified pattern 2026-10-05."""
        msg = build_godown_message(name, "Alter", new_name=new_name)
        return self.import_masters(msg, "Alter", "Godown", name,
                                   preview=preview, confirm_token=confirm_token)

    def list_godowns(self, company: Optional[str] = None) -> dict:
        """List all godowns (read-only)."""
        collection_id = "ListGodown"
        envelope = build_collection_export_envelope(
            collection_id, "Godown", ["Name"], company=company
        )
        try:
            r = requests.post(
                self.base_url, data=envelope.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        t = r.content.decode("utf-8", "replace")
        names = re.findall(r"<NAME>([^<]+)</NAME>", t)
        return {"success": True, "count": len(names), "godowns": sorted(set(names))}

    # -- unit -------------------------------------------------------------

    def create_unit(self, name: str, preview: bool = True,
                      confirm_token: str = "",
                      formal_name: str = "") -> ImportResult:
        """Create a simple unit of measure (verified 2026-10-03)."""
        msg = build_unit_message(name, "Create", formal_name=formal_name)
        return self.import_masters(msg, "Create", "Unit", name,
                                   preview=preview, confirm_token=confirm_token)

    def alter_unit(self, name: str, preview: bool = True,
                   confirm_token: str = "",
                   formal_name: str = "") -> ImportResult:
        """Alter a unit of measure (e.g. set formal name)."""
        msg = build_unit_message(name, "Alter", formal_name=formal_name)
        return self.import_masters(msg, "Alter", "Unit", name,
                                   preview=preview, confirm_token=confirm_token)

    def delete_unit(self, name: str, preview: bool = True,
                     confirm_token: str = "") -> ImportResult:
        """Delete a unit after exact-target validation."""
        return self._delete_master(
            "Unit", name,
            lambda company: wrap_import_envelope(build_unit_message(name, "Delete"), company),
            lambda company: self._validate_exact_master("Unit", name, company),
            preview=preview, confirm_token=confirm_token)

    # -- purchase voucher -------------------------------------------------

    def list_units(self, company: Optional[str] = None) -> dict:
        """List all units (read-only)."""
        envelope = build_collection_export_envelope(
            "ListUnit", "Unit", ["Name"], company=company
        )
        try:
            r = requests.post(
                self.base_url, data=envelope.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        t = r.content.decode("utf-8", "replace")
        names = re.findall(r"<NAME>([^<]+)</NAME>", t)
        return {"success": True, "count": len(names), "units": sorted(set(names))}

    def create_purchase_voucher(
        self,
        date: str,
        party_name: str,
        items: list,
        charges: Optional[list] = None,
        reference: str = "",
        narration: str = "",
        party_address_lines: Optional[list] = None,
        party_state: str = "",
        party_country: str = "",
        party_pincode: str = "",
        party_gstin: str = "",
        voucher_number: str = "",
        remote_id: str = "",
        preview: bool = True,
        confirm_token: str = "",
    ) -> ImportResult:
        """Create a Purchase item-invoice voucher with inventory + charges.

        Verified 2026-09-29 against live Tally Prime. See
        build_purchase_voucher_message() for the full verified tag mapping.
        """
        try:
            msg = build_purchase_voucher_message(
                date=date,
                party_name=party_name,
                items=items,
                charges=charges,
                reference=reference,
                narration=narration,
                party_address_lines=party_address_lines,
                party_state=party_state,
                party_country=party_country,
                party_pincode=party_pincode,
                party_gstin=party_gstin,
                voucher_number=voucher_number,
                remote_id=remote_id,
            )
        except ValueError as e:
            return self._mutation_refused(
                "Create", "PurchaseVoucher", party_name,
                f"REFUSED: {e}")
        return self.import_masters(
            msg, "Create", "PurchaseVoucher", party_name, preview=preview, confirm_token=confirm_token
        )

    def delete_voucher(self, voucher_type: str, remote_id: str = "",
                       date: str = "", voucher_number: str = "",
                       preview: bool = True,
                       confirm_token: str = "") -> ImportResult:
        """Permanently DELETE a voucher via XML (verified pattern:
        UPPERCASE ACTION="Delete" via Import Data, REPORTNAME=Vouchers).

        remote_id is required -- it's the only reliable identifier.
        UI-created vouchers (Tally-internal IDs) can't be deleted via XML;
        delete those in Tally's UI.

        Safety: the voucher target is verified against the Day Book of the
        pinned company before previewing (and re-checked before execution),
        so 'date' (YYYYMMDD) is required. Without it the delete fails
        closed. The "Your Company" fallback was removed: the pinned
        TALLY_COMPANY is always used.
        """
        rid = (remote_id or "").strip()
        if not rid:
            return ImportResult(
                success=False, action="Delete", object_type="Voucher",
                name=voucher_number or voucher_type, preview=preview, xml="",
                message=("REFUSED: remote_id is required -- without a REMOTEID "
                         "a voucher cannot be deleted via XML. Find the "
                         "voucher's REMOTEID in the Day Book export."),
            )
        try:
            company = get_pinned_company()
        except CompanyNotConfiguredError as e:
            return self._mutation_refused(
                "Delete", "Voucher", rid, f"REFUSED: {e}")
        d = (date or "").strip()
        if not d:
            return self._mutation_refused(
                "Delete", "Voucher", rid,
                "REFUSED: 'date' (YYYYMMDD) is required so the voucher can "
                "be verified in the Day Book before deletion. Failing "
                "closed -- pass the voucher's date.",
                company=company)
        target_id, err = self._validate_voucher_target(
            voucher_type, rid, d, company)
        if err:
            return self._mutation_refused(
                "Delete", "Voucher", rid, err, company=company)
        return self._delete_voucher_validated(
            voucher_type, rid, d, voucher_number,
            company, preview, confirm_token)

    def _validate_voucher_target(self, voucher_type: str, remote_id: str,
                                 date: str, company: str) -> tuple:
        """Verify a voucher by REMOTEID in the Day Book of one date.

        Returns (target_id, error). Fails closed when the voucher is
        missing, ambiguous, of a different type, or unverifiable.
        """
        d = yyyymmdd(date)
        env = (
            "<ENVELOPE><HEADER><VERSION>1</VERSION>"
            "<TALLYREQUEST>Export</TALLYREQUEST>"
            "<TYPE>Data</TYPE><ID>Day Book</ID></HEADER>"
            "<BODY><DESC><STATICVARIABLES>"
            "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
            f"<SVCURRENTCOMPANY>{xml_escape(company)}</SVCURRENTCOMPANY>"
            f"<SVFROMDATE>{d}</SVFROMDATE>"
            f"<SVTODATE>{d}</SVTODATE>"
            "</STATICVARIABLES></DESC></BODY></ENVELOPE>"
        )
        try:
            r = requests.post(
                self.base_url, data=env.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=self.timeout)
        except requests.RequestException as e:
            return "", (f"REFUSED: cannot reach Tally to verify the voucher "
                        f"target ({e}). Failing closed.")
        try:
            root = _parse_xml_safe(r.text)
        except (UnsafeXmlError, XmlParseError) as e:
            return "", (f"REFUSED: could not parse the Day Book response "
                        f"({e}). Failing closed.")
        hits = []
        for v in _find_all_by_local(root, "VOUCHER"):
            vrid = (v.get("REMOTEID") or "").strip()
            if vrid == remote_id:
                vtype = (v.get("VCHTYPE") or "").strip()
                hits.append(vtype)
        if not hits:
            return "", (f"REFUSED: no voucher with REMOTEID '{remote_id}' "
                        f"found on {d} in company '{company}' (exact match).")
        if len(hits) > 1:
            return "", (f"REFUSED: {len(hits)} vouchers share REMOTEID "
                        f"'{remote_id}' -- ambiguous target.")
        if voucher_type and hits[0].lower() != voucher_type.strip().lower():
            return "", (f"REFUSED: REMOTEID '{remote_id}' belongs to a "
                        f"'{hits[0]}' voucher, not '{voucher_type}'.")
        # Re-check note: _gated_send re-runs this validator via the
        # delete flow below before dispatch.
        return remote_id, ""

    def _delete_voucher_validated(self, voucher_type: str, remote_id: str,
                                  date: str, voucher_number: str,
                                  company: str, preview: bool,
                                  confirm_token: str) -> ImportResult:
        """Delete-voucher flow with target re-validation before dispatch."""
        target_id, err = self._validate_voucher_target(
            voucher_type, remote_id, date, company)
        if err:
            return self._mutation_refused(
                "Delete", "Voucher", remote_id, err, company=company)
        msg = build_voucher_delete_message(
            voucher_type=voucher_type, remote_id=remote_id,
            date=date, voucher_number=voucher_number)
        envelope = _VOUCHER_ENVELOPE.format(
            body=msg, company=xml_escape(company))
        op = "Delete:Voucher"
        extra = {"target": target_id}
        if preview:
            token, exp = self._gate.issue(
                op, company, _payload_hash(envelope),
                f"Delete Voucher '{remote_id}'", extra)
            return ImportResult(
                success=True, action="Delete", object_type="Voucher",
                name=remote_id, preview=True, xml=envelope, company=company,
                confirm_token=token, confirm_token_expires_at=exp,
                message=(
                    f"PREVIEW only -- nothing sent to Tally. Would delete "
                    f"{voucher_type} voucher '{target_id}' in company "
                    f"'{company}'. Target verified in the Day Book for "
                    f"{yyyymmdd(date)}. To execute, call again with "
                    f"preview=False and the confirm_token from this preview. "
                    f"The target is re-checked immediately before execution."
                ),
            )
        try:
            self._gate.consume(
                op, company, _payload_hash(envelope), confirm_token, extra)
        except MutationTokenError as e:
            return self._mutation_refused(
                "Delete", "Voucher", remote_id, f"REFUSED: {e}",
                xml=envelope, company=company)
        target_id2, err2 = self._validate_voucher_target(
            voucher_type, remote_id, date, company)
        if err2 or target_id2 != target_id:
            return self._mutation_refused(
                "Delete", "Voucher", remote_id,
                "REFUSED: the voucher target changed or became unverifiable "
                "since the preview. Preview again for a fresh token.",
                xml=envelope, company=company)
        return self._post_import(envelope, "Delete", "Voucher", remote_id,
                                 company)

    def create_sales_voucher(
        self,
        date: str,
        party_name: str,
        sale_type: str = "credit",
        items: list = None,
        charges: list = None,
        reference: str = "",
        narration: str = "",
        party_address_lines: list = None,
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
    ) -> ImportResult:
        """Create a Sales item-invoice voucher with inventory + charges/tax.

        Mirror of purchase: customer entry negative, items/charges positive.
        sale_type="credit" -> customer ledger + BILLALLOCATIONS;
        sale_type="cash" -> cash ledger, no bill allocations.
        buyer_name: cash sale me bill pe print hone wala naam
        (ledger wahi rahega, naya ledger nahi banega).
        """
        try:
            msg = build_sales_voucher_message(
                date=date,
                party_name=party_name,
                sale_type=sale_type,
                items=items or [],
                charges=charges,
                reference=reference,
                narration=narration,
                party_address_lines=party_address_lines,
                party_state=party_state,
                party_country=party_country,
                party_pincode=party_pincode,
                party_gstin=party_gstin,
                voucher_number=voucher_number,
                remote_id=remote_id,
                sales_ledger=sales_ledger,
                buyer_name=buyer_name,
            )
        except ValueError as e:
            return self._mutation_refused(
                "Create", "SalesVoucher", party_name,
                f"REFUSED: {e}")
        return self.import_masters(
            msg, "Create", "SalesVoucher", party_name, preview=preview, confirm_token=confirm_token
        )

    def create_payment_voucher(
        self,
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
    ) -> ImportResult:
        """Create a Payment accounting voucher (no inventory).

        debit_entries: list of {ledger, amount, bill_ref (optional),
        is_party (bool)}. Party payments with bill_ref get Agst Ref
        bill allocation for bill-wise settlement.
        credit_ledger: Cash/Bank ledger (default "Cash").
        bank_transaction_type: e.g. "Others", "Cheque" -- adds
            BANKALLOCATIONS to the bank ledger entry (optional).
        """
        try:
            msg = build_payment_voucher_message(
                date=date,
                debit_entries=debit_entries,
                credit_ledger=credit_ledger,
                reference=reference,
                narration=narration,
                voucher_number=voucher_number,
                remote_id=remote_id,
                bank_transaction_type=bank_transaction_type,
            )
        except ValueError as e:
            return self._mutation_refused(
                "Create", "PaymentVoucher", reference or credit_ledger,
                f"REFUSED: {e}")
        return self.import_masters(
            msg, "Create", "PaymentVoucher", reference or credit_ledger,
            preview=preview, confirm_token=confirm_token,
        )

    def create_receipt_voucher(
        self,
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
    ) -> ImportResult:
        """Create a Receipt accounting voucher (no inventory).

        Mirror of payment: money comes IN.
        credit_entries: list of {ledger, amount, bill_ref (optional),
        is_party (bool)}. Party receipts with bill_ref get Agst Ref
        bill allocation for bill-wise settlement.
        debit_ledger: Cash/Bank ledger (default "Cash").
        bank_transaction_type: e.g. "Others", "Cheque" -- adds
            BANKALLOCATIONS to the bank ledger entry (optional).
        """
        try:
            msg = build_receipt_voucher_message(
                date=date,
                credit_entries=credit_entries,
                debit_ledger=debit_ledger,
                reference=reference,
                narration=narration,
                voucher_number=voucher_number,
                remote_id=remote_id,
                bank_transaction_type=bank_transaction_type,
            )
        except ValueError as e:
            return self._mutation_refused(
                "Create", "ReceiptVoucher", reference or debit_ledger,
                f"REFUSED: {e}")
        return self.import_masters(
            msg, "Create", "ReceiptVoucher", reference or debit_ledger,
            preview=preview, confirm_token=confirm_token,
        )

    def create_contra_voucher(
        self,
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
    ) -> ImportResult:
        """Create a Contra voucher (cash/bank fund transfer, no inventory).

        debit_ledger: Destination (where money goes TO, e.g. "PNB Bank A/c").
        credit_ledger: Source (where money comes FROM, e.g. "Cash").
        amount: Positive transfer amount.
        bank_transaction_type: e.g. "Others" -- BANKALLOCATIONS (optional).
        """
        try:
            msg = build_contra_voucher_message(
                date=date,
                debit_ledger=debit_ledger,
                credit_ledger=credit_ledger,
                amount=amount,
                reference=reference,
                narration=narration,
                voucher_number=voucher_number,
                remote_id=remote_id,
                bank_transaction_type=bank_transaction_type,
            )
        except ValueError as e:
            return self._mutation_refused(
                "Create", "ContraVoucher", reference or debit_ledger,
                f"REFUSED: {e}")
        return self.import_masters(
            msg, "Create", "ContraVoucher", reference or debit_ledger,
            preview=preview, confirm_token=confirm_token,
        )

    def create_journal_voucher(
        self,
        date: str,
        debit_entries: list,
        credit_entries: list,
        reference: str = "",
        narration: str = "",
        voucher_number: str = "",
        remote_id: str = "",
        preview: bool = True,
        confirm_token: str = "",
    ) -> ImportResult:
        """Create a Journal voucher (adjustments, no cash/bank, no inventory).

        debit_entries / credit_entries: list of {"ledger": str,
        "amount": float (positive), "bill_allocations": [{"bill_name": str,
        "bill_type": "New Ref"|"Agst Ref"|"On Account"}]}.
        Signed amounts must net to zero.
        """
        try:
            msg = build_journal_voucher_message(
                date=date,
                debit_entries=debit_entries,
                credit_entries=credit_entries,
                reference=reference,
                narration=narration,
                voucher_number=voucher_number,
                remote_id=remote_id,
            )
        except ValueError as e:
            return self._mutation_refused(
                "Create", "JournalVoucher", reference or "Journal",
                f"REFUSED: {e}")
        return self.import_masters(
            msg, "Create", "JournalVoucher", reference or "Journal",
            preview=preview, confirm_token=confirm_token,
        )

    def create_debit_note(
        self,
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
    ) -> ImportResult:
        """Create a Debit Note (purchase return, stock out)."""
        try:
            msg = build_debit_note_message(
                date=date, party_name=party_name, items=items, charges=charges,
                reference=reference, narration=narration,
                voucher_number=voucher_number, remote_id=remote_id,
            )
        except ValueError as e:
            return self._mutation_refused(
                "Create", "DebitNote", reference or party_name,
                f"REFUSED: {e}")
        return self.import_masters(
            msg, "Create", "DebitNote", reference or party_name,
            preview=preview, confirm_token=confirm_token,
        )

    def create_credit_note(
        self,
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
    ) -> ImportResult:
        """Create a Credit Note (sales return, stock in)."""
        try:
            msg = build_credit_note_message(
                date=date, party_name=party_name, items=items, charges=charges,
                reference=reference, narration=narration,
                voucher_number=voucher_number, remote_id=remote_id,
            )
        except ValueError as e:
            return self._mutation_refused(
                "Create", "CreditNote", reference or party_name,
                f"REFUSED: {e}")
        return self.import_masters(
            msg, "Create", "CreditNote", reference or party_name,
            preview=preview, confirm_token=confirm_token,
        )

    # -- internal --------------------------------------------------------

    def sync_masters(self, preview: bool = False) -> dict:
        """Fetch all ledgers + stock items from Tally into local cache.

        Step 0: checks Tally connection first -- sync only runs if Tally
        is reachable. Saves to sync/masters.json with timestamp. Use at
        session start or when masters change in Tally UI.
        """
        if preview:
            return {
                "success": True, "preview": True,
                "message": "PREVIEW -- would check Tally connection, then fetch ledgers + stock items.",
            }
        # Step 0: Tally connector check
        try:
            conn = self.check_connection()
            reachable = conn.get("reachable", False) if isinstance(conn, dict) else False
            if not reachable:
                return {"success": False,
                        "message": "Tally not reachable -- sync cancelled. Is Tally Prime running with port 9999?"}
        except Exception as e:
            return {"success": False, "message": f"Tally connection check failed: {e}"}
        try:
            data = _do_sync(self)
            return {
                "success": True,
                "ledgers": len(data["ledgers"]),
                "stock_items": len(data["stock_items"]),
                "synced_at": data["_synced_at_str"],
                "message": (f"Synced {len(data['ledgers'])} ledgers + "
                            f"{len(data['stock_items'])} stock items."),
            }
        except Exception as e:
            return {"success": False, "message": f"Sync failed: {e}"}

    def check_ledger_exists(self, name: str, refresh: bool = False) -> dict:
        """Check if a ledger exists. Auto-refreshes cache if stale (>4h).

        refresh=True forces a fresh sync from Tally first.
        Name match is case-insensitive.
        """
        q = name.strip().lower()
        data, was_refreshed = _ensure_fresh(self, force=refresh)
        if not data:
            return {"success": False, "exists": False,
                    "message": "No cached data and sync failed."}
        age_h = (_time.time() - data.get("_synced_at", 0)) / 3600
        found = None
        for lg in data["ledgers"]:
            if lg["name"].strip().lower() == q:
                found = lg
                break
        return {
            "success": True,
            "exists": found is not None,
            "ledger": found,
            "data_age_hours": round(age_h, 1),
            "was_refreshed": was_refreshed,
            "message": (f"Ledger '{name}' {'EXISTS' if found else 'NOT FOUND'} "
                        f"(data {round(age_h, 1)}h old"
                        f"{', just refreshed' if was_refreshed else ''})."),
        }

    def check_stockitem_exists(self, name: str, refresh: bool = False) -> dict:
        """Check if a stock item exists. Auto-refreshes cache if stale (>4h).

        refresh=True forces a fresh sync from Tally first.
        Name match is case-insensitive.
        """
        q = name.strip().lower()
        data, was_refreshed = _ensure_fresh(self, force=refresh)
        if not data:
            return {"success": False, "exists": False,
                    "message": "No cached data and sync failed."}
        age_h = (_time.time() - data.get("_synced_at", 0)) / 3600
        found = None
        for it in data["stock_items"]:
            if it["name"].strip().lower() == q:
                found = it
                break
        return {
            "success": True,
            "exists": found is not None,
            "stock_item": found,
            "data_age_hours": round(age_h, 1),
            "was_refreshed": was_refreshed,
            "message": (f"Stock item '{name}' {'EXISTS' if found else 'NOT FOUND'} "
                        f"(data {round(age_h, 1)}h old"
                        f"{', just refreshed' if was_refreshed else ''})."),
        }

    # -- internal --------------------------------------------------------

    @staticmethod
    def _attach_format_warnings(res: ImportResult, kwargs: dict) -> None:
        pan = kwargs.get("pan")
        gstin = kwargs.get("gstin")
        if pan:
            w = _pan_warning(pan)
            if w:
                res.warnings.append(w)
        if gstin:
            w = _gstin_warning(gstin)
            if w:
                res.warnings.append(w)
