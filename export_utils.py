"""Build Excel (.xlsx) and PDF files from a list of result rows, in memory.

Kept dependency-light so it works on Render and Vercel (no disk writes — the
files are returned as BytesIO buffers and streamed straight to the browser).
"""
import io

from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph

# Excel handles any size easily. A giant PDF (tens of thousands of rows) is slow
# to build and unwieldy to read, so the PDF is capped and notes the truncation.
PDF_MAX_ROWS = 800


def rows_to_excel(rows, cols):
    """Return a BytesIO .xlsx with a header row + every data row."""

    wb = Workbook()
    ws = wb.active
    ws.title = "data"

    ws.append(cols)
    for r in rows:
        ws.append([r.get(c) for c in cols])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def rows_to_pdf(rows, cols, title="data"):
    """Return a BytesIO PDF table (landscape, wrapping cells)."""

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=18, rightMargin=18, topMargin=22, bottomMargin=18,
    )

    cell = ParagraphStyle("cell", fontName="Helvetica", fontSize=6, leading=7)
    head = ParagraphStyle("head", fontName="Helvetica-Bold", fontSize=6,
                           leading=7, textColor=colors.white)

    shown = rows[:PDF_MAX_ROWS]
    data = [[Paragraph(str(c), head) for c in cols]]
    for r in shown:
        data.append([Paragraph(str(r.get(c, "")), cell) for c in cols])

    # distribute columns evenly across the usable page width
    usable = landscape(A4)[0] - 36
    col_w = usable / max(len(cols), 1)

    table = Table(data, colWidths=[col_w] * len(cols), repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563eb")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f3f6fb")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))

    title_style = ParagraphStyle("title", fontName="Helvetica-Bold",
                                 fontSize=12, spaceAfter=8)
    elements = [Paragraph(f"{title} — {len(rows)} record(s)", title_style), table]

    if len(rows) > PDF_MAX_ROWS:
        note = ParagraphStyle("note", fontName="Helvetica-Oblique",
                              fontSize=7, spaceBefore=6)
        elements.append(Paragraph(
            f"Showing the first {PDF_MAX_ROWS} of {len(rows)} rows. "
            f"Download the Excel file for the complete data.", note))

    doc.build(elements)
    buf.seek(0)
    return buf
