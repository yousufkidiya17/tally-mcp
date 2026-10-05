# Tally Agent — Technical Guide

**How Tally Agent talks to Tally Prime via XML API**

---

## Connection

```
URL: http://localhost:9999/
Method: POST
Content-Type: text/xml
```

Tally Prime must be running with a company open. The XML API is enabled by default on port 9999.

### Transport security

Tally's XML API is **plain HTTP**: no transport encryption, no
authentication. Anyone who can reach the port can read and write the
company. Operate it on `localhost` or a trusted private network only.
For remote access, front port 9999 with an authenticated,
TLS-terminating reverse proxy (client certificates or equivalent).
The confirm_token gate protects the MCP tool flow; it does NOT secure an
exposed Tally endpoint against other HTTP clients.

### Mutation safety

- Every import envelope carries `<SVCURRENTCOMPANY>` pinned to the
  validated `TALLY_COMPANY` env var (fail-closed when unset).
- Writes are two-step: `preview=True` returns the exact XML plus a
  short-lived single-use `confirm_token`; `preview=False` requires that
  token, which the server verifies (operation, company, payload hash,
  expiry, single-use) before dispatching exactly once.
- Deletes validate the exact target against a fresh export of the pinned
  company before previewing, bind the target into the token, and re-check
  immediately before execution.
- Responses are parsed with a real XML parser (no regex extraction):
  CDATA, entities, repeated elements and namespaces are handled;
  DTD/entity declarations are rejected; malformed responses are failures,
  never successes.

---

## Sending Data TO Tally (Import)

### Envelope Structure

Every request uses this envelope:

```xml
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Import</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>Vouchers</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>Your Company Name</SVCURRENTCOMPANY>
      </STATICVARIABLES>
    </DESC>
    <DATA>
      <TALLYMESSAGE>
        <!-- Your voucher/master XML here -->
      </TALLYMESSAGE>
    </DATA>
  </BODY>
</ENVELOPE>
```

### Example: Create a Ledger

```xml
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Import</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>All Masters</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>Your Company Name</SVCURRENTCOMPANY>
      </STATICVARIABLES>
    </DESC>
    <DATA>
      <TALLYMESSAGE>
        <LEDGER NAME="Test Customer" Action="Create">
          <NAME>Test Customer</NAME>
          <PARENT>Sundry Debtors</PARENT>
          <OPENINGBALANCE>5000</OPENINGBALANCE>
        </LEDGER>
      </TALLYMESSAGE>
    </DATA>
  </BODY>
</ENVELOPE>
```

### Example: Create a Sales Voucher

```xml
<TALLYMESSAGE>
  <VOUCHER VCHTYPE="Sales" Action="Create">
    <DATE>20240401</DATE>
    <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
    <VOUCHERNUMBER>SALE-001</VOUCHERNUMBER>
    <PARTYLEDGERNAME>Customer Name</PARTYLEDGERNAME>
    <ALLLEDGERENTRIES.LIST>
      <LEDGERNAME>Customer Name</LEDGERNAME>
      <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
      <AMOUNT>-9020</AMOUNT>
    </ALLLEDGERENTRIES.LIST>
    <ALLLEDGERENTRIES.LIST>
      <LEDGERNAME>Sales A/c</LEDGERNAME>
      <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
      <AMOUNT>9020</AMOUNT>
    </ALLLEDGERENTRIES.LIST>
  </VOUCHER>
</TALLYMESSAGE>
```

### Response Format

```xml
<RESPONSE>
  <CREATED>1</CREATED>
  <ALTERED>0</ALTERED>
  <DELETED>0</DELETED>
  <ERRORS>0</ERRORS>
</RESPONSE>
```

- `CREATED=1` → Success
- `ERRORS>0` → Check `<LINEERROR>` for details

### Delete Pattern (Important!)

True delete needs **UPPERCASE** `ACTION="Delete"`:

```xml
<LEDGER NAME="Test" ACTION="Delete">
  <NAME>Test</NAME>
</LEDGER>
```

---

## Receiving Data FROM Tally (Export)

### Envelope Structure

```xml
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>Day Book</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        <SVCURRENTCOMPANY>Your Company Name</SVCURRENTCOMPANY>
        <SVFROMDATE>20240401</SVFROMDATE>
        <SVTODATE>20260430</SVTODATE>
      </STATICVARIABLES>
    </DESC>
  </BODY>
</ENVELOPE>
```

### Common Report IDs

| Report ID | Returns |
|-----------|---------|
| `Day Book` | All vouchers |
| `Trial Balance` | Ledger balances (DSP format) |
| `Profit and Loss` | P&L (PLAMT format) |
| `Balance Sheet` | BS (BSAMT format) |
| `Cash Flow` | Monthly cash flow |
| `Bills Receivable` | Customer bills |
| `Bills Payable` | Supplier bills |
| `GST Computation` | GST summary |

### Response Parsing

**Vouchers** — Look for:
```xml
<VOUCHER VCHTYPE="Sales" ...>
  <DATE>20240401</DATE>
  <VOUCHERNUMBER>SALE-001</VOUCHERNUMBER>
  <PARTYLEDGERNAME>Customer</PARTYLEDGERNAME>
  <ALLLEDGERENTRIES.LIST>
    <LEDGERNAME>...</LEDGERNAME>
    <AMOUNT>...</AMOUNT>
    <ISDEEMEDPOSITIVE>Yes/No</ISDEEMEDPOSITIVE>
  </ALLLEDGERENTRIES.LIST>
</VOUCHER>
```

**Important:** Tags may have `TYPE` attributes:
```xml
<!-- Both are possible -->
<PARTYLEDGERNAME>Customer</PARTYLEDGERNAME>
<PARTYLEDGERNAME TYPE="String">Customer</PARTYLEDGERNAME>
```
Always use regex: `<PARTYLEDGERNAME[^>]*>([^<]+)</PARTYLEDGERNAME>`

**Trial Balance** — DSP format:
```xml
<DSPACCNAME>
  <DSPDISPNAME>Sundry Debtors</DSPDISPNAME>
</DSPACCNAME>
<DSPACCINFO>
  <DSPCLDRAMT>
    <DSPCLDRAMTA>18000.00</DSPCLDRAMTA>
  </DSPCLDRAMT>
</DSPACCINFO>
```

**Bills** — Bill-wise:
```xml
<BILLFIXED>
  <BILLDATE>1-Apr-26</BILLDATE>
  <BILLREF>INV-001</BILLREF>
  <BILLPARTY>Customer</BILLPARTY>
</BILLFIXED>
<BILLCL>5000.00</BILLCL>
```

---

## Key Gotchas

1. **Date format:** Always `YYYYMMDD` (e.g. `20240401`)
2. **TYPE attributes:** Tags often have `TYPE="String"` etc. Use `[^>]*` in regex
3. **VOUCHER vs VOUCHERTYPE:** Regex `<VOUCHER[\s>]` to avoid matching `<VOUCHERTYPE>`
4. **Amount signs:** Negative = Dr, Positive = Cr (check ISDEEMEDPOSITIVE)
5. **Educational Mode:** Only 1st, 2nd, last day of month allowed for vouchers
6. **GSTIN tags:** May have `&#4;` prefix — strip it
7. **Empty responses:** `<ENVELOPE></ENVELOPE>` means no data or wrong REPORTNAME

---

## Python Example

```python
import requests

# Send to Tally
xml = """<ENVELOPE>...</ENVELOPE>"""
r = requests.post(
    "http://localhost:9999/",
    data=xml.encode("utf-8"),
    headers={"Content-Type": "text/xml"},
    timeout=30
)

# Parse response
import re
created = re.search(r"<CREATED>(\d+)</CREATED>", r.text)
if created and created.group(1) == "1":
    print("Success!")
```

---

## Architecture

```
AI Agent → MCP Tools → tally_client.py → HTTP POST → Tally Prime:9999
                ↓
         XML Request/Response
                ↓
         Parse with regex → Python dict → Agent
```

**tally_client.py** handles all XML building and parsing.
**server.py** exposes 69 MCP tools using the client.
