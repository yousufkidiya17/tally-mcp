"""Tally Agent PDF generation - Sales Invoice.

Uses reportlab to generate professional GST invoices from Tally voucher data.
Verified 2026-10-05.
"""

import os
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
)

try:
    from num2words import num2words
    HAS_NUM2WORDS = True
except ImportError:
    HAS_NUM2WORDS = False


def inr(amount) -> str:
    """Format amount in Indian numbering (12,34,567.00)."""
    try:
        d = Decimal(str(amount)).quantize(Decimal("0.01"))
    except Exception:
        return "0.00"
    s = f"{d:.2f}"
    int_part, dec_part = s.split(".")
    # Indian grouping: last 3, then groups of 2
    if len(int_part) > 3:
        last3 = int_part[-3:]
        rest = int_part[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        int_part = ",".join(groups) + "," + last3
    return f"{int_part}.{dec_part}"


def inr_words(amount) -> str:
    """Amount in words (Indian)."""
    if not HAS_NUM2WORDS:
        return ""
    try:
        words = num2words(float(amount), lang="en_IN", to="currency", currency="INR")
        # Normalize
        words = words.replace("euro", "").strip()
        return f"Rupees {words} Only"
    except Exception:
        return ""


def export_sales_invoice_pdf(voucher_data: dict, output_path: str,
                             company: dict = None) -> str:
    """Generate a GST sales invoice PDF.

    Args:
        voucher_data: {"voucher_number": ..., "date": ..., "party": ...,
                       "items": [{"name": ..., "qty": ..., "rate": ..., "amount": ...}],
                       "cgst": ..., "sgst": ..., "igst": ...,
                       "total": ..., "narration": ...}
        output_path: Where to save the PDF.
        company: {"name": ..., "address": ..., "gstin": ..., "phone": ...}

    Returns the output path.
    """
    company = company or {}
    comp_name = company.get("name", os.environ.get("TALLY_COMPANY", "Your Company Name"))
    comp_addr = company.get("address", "")
    comp_gstin = company.get("gstin", "")
    comp_phone = company.get("phone", "")

    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm, bottomMargin=15*mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Title2", parent=styles["Title"], fontSize=18)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=12)
    normal = styles["Normal"]
    small = ParagraphStyle("Small", parent=normal, fontSize=8)
    right = ParagraphStyle("Right", parent=normal, alignment=2)

    story = []

    # Title
    story.append(Paragraph("TAX INVOICE", title_style))
    story.append(Spacer(1, 5*mm))

    # Company block
    story.append(Paragraph(f"<b>{comp_name}</b>", h2))
    story.append(Paragraph(comp_addr, normal))
    if comp_gstin:
        story.append(Paragraph(f"GSTIN: {comp_gstin}", normal))
    if comp_phone:
        story.append(Paragraph(f"Phone: {comp_phone}", normal))
    story.append(Spacer(1, 5*mm))

    # Bill to + Invoice meta
    vnum = voucher_data.get("voucher_number", "")
    vdate = voucher_data.get("date", "")
    # Format date
    if len(str(vdate)) == 8 and str(vdate).isdigit():
        vdate = f"{vdate[6:8]}-{vdate[4:6]}-{vdate[0:4]}"
    party = voucher_data.get("party", "")

    meta_data = [
        [Paragraph(f"<b>Bill To:</b><br/>{party}", normal),
         Paragraph(f"<b>Invoice No:</b> {vnum}<br/><b>Date:</b> {vdate}", normal)],
    ]
    meta_table = Table(meta_data, colWidths=[85*mm, 85*mm])
    meta_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 5*mm))

    # Items table
    items = voucher_data.get("items", [])
    item_data = [
        ["S.No", "Item Description", "Qty", "Rate", "Amount"],
    ]
    for i, it in enumerate(items, 1):
        item_data.append([
            str(i),
            it.get("name", ""),
            str(it.get("qty", "")),
            inr(it.get("rate", 0)),
            inr(it.get("amount", 0)),
        ])

    # Totals
    subtotal = sum(float(it.get("amount", 0)) for it in items)
    cgst = float(voucher_data.get("cgst", 0))
    sgst = float(voucher_data.get("sgst", 0))
    igst = float(voucher_data.get("igst", 0))
    total = float(voucher_data.get("total", subtotal + cgst + sgst + igst))

    item_data.append(["", "", "", "Subtotal", inr(subtotal)])
    if cgst:
        item_data.append(["", "", "", "CGST", inr(cgst)])
    if sgst:
        item_data.append(["", "", "", "SGST", inr(sgst)])
    if igst:
        item_data.append(["", "", "", "IGST", inr(igst)])
    item_data.append(["", "", "", "<b>Total</b>", f"<b>Rs. {inr(total)}</b>"])

    # Convert to Paragraphs for wrapping
    pdata = []
    for row in item_data:
        prow = []
        for j, cell in enumerate(row):
            if j == 1:  # description - allow wrapping
                prow.append(Paragraph(cell, small))
            else:
                prow.append(Paragraph(cell, small))
        pdata.append(prow)

    itable = Table(pdata, colWidths=[12*mm, 80*mm, 20*mm, 28*mm, 30*mm],
                   repeatRows=1)
    itable.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("BOX", (0, 0), (-1, -1), 1, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(itable)
    story.append(Spacer(1, 5*mm))

    # Amount in words
    words = inr_words(total)
    if words:
        story.append(Paragraph(f"<b>Amount in words:</b> {words}", normal))
        story.append(Spacer(1, 5*mm))

    # Narration
    narration = voucher_data.get("narration", "")
    if narration:
        story.append(Paragraph(f"<b>Narration:</b> {narration}", small))
        story.append(Spacer(1, 5*mm))

    # Signature
    story.append(Spacer(1, 10*mm))
    story.append(Paragraph("Authorised Signatory", right))

    doc.build(story)
    return output_path
