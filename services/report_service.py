"""
Report rendering: branded PDF (reportlab) and CSV. Consumes the dicts produced by
services/report_data.py. The PDF header carries the school logo (read from the
configured persistent storage), name, motto and contact details, then the
session / term / class / student / date block and the attendance summary.
"""

import csv
import io
import logging
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

from services import storage_service

log = logging.getLogger(__name__)
BRAND = colors.HexColor("#0b3d5c")
ACCENT = colors.HexColor("#b8860b")


def load_logo_bytes(cfg, settings):
    """Logo bytes from persistent storage, or None (reports still render without it)."""
    key = settings["logo_url"] if settings else None
    if not key:
        return None
    try:
        return storage_service.get_backend(cfg).read(key)
    except Exception:  # noqa: BLE001 - never break a report because of the logo
        log.warning("Could not load school logo for report", exc_info=True)
        return None


def export_csv(report, school_name) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["School", "Academic Session", "Term"] + report["columns"])
    for row in report["rows"]:
        w.writerow([school_name, report["session_name"], report["term_name"]] + list(row))
    return buf.getvalue()


def _footer_factory(school_name):
    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 10 * mm, f"{school_name} — attendance report")
        canvas.drawRightString(doc.pagesize[0] - 15 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()
    return footer


def render_pdf(settings, logo_bytes, report) -> bytes:
    wide = len(report["columns"]) > 7
    pagesize = landscape(A4) if wide else A4
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=pagesize, topMargin=14 * mm, bottomMargin=18 * mm,
                            leftMargin=15 * mm, rightMargin=15 * mm)
    styles = getSampleStyleSheet()
    name_style = ParagraphStyle("n", parent=styles["Title"], fontSize=16, textColor=BRAND, alignment=0, spaceAfter=1)
    small = ParagraphStyle("s", parent=styles["Normal"], fontSize=8, textColor=colors.grey, leading=10)
    motto = ParagraphStyle("m", parent=styles["Normal"], fontSize=9, textColor=ACCENT)
    title = ParagraphStyle("t", parent=styles["Heading2"], textColor=colors.black, spaceBefore=8, spaceAfter=6)
    cell = ParagraphStyle("c", parent=styles["Normal"], fontSize=8, leading=10)

    text = [Paragraph(escape(settings["school_name"]), name_style)]
    if settings["motto"]:
        text.append(Paragraph(escape(settings["motto"]), motto))
    contact = " | ".join(escape(str(settings[k])) for k in ("address", "phone", "email", "website") if settings[k])
    if contact:
        text.append(Paragraph(contact, small))
    header_cells = []
    if logo_bytes:
        try:
            reader = ImageReader(io.BytesIO(logo_bytes))
            w, h = reader.getSize()
            scale = min(24 * mm / w, 24 * mm / h)
            header_cells.append(Image(io.BytesIO(logo_bytes), width=w * scale, height=h * scale))
        except Exception:  # noqa: BLE001
            log.warning("Logo could not be rendered in PDF", exc_info=True)
    header_cells.append(text)
    header = Table([header_cells], colWidths=[28 * mm, None] if len(header_cells) == 2 else [None])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))

    flow = [header, Spacer(1, 4), Paragraph(escape(report["title"]), title)]
    meta_rows = [[Paragraph(f"<b>{escape(k)}:</b> {escape(str(v))}", cell)] for k, v in report["meta"]]
    flow.append(Table(meta_rows, colWidths=[None]))
    flow.append(Spacer(1, 6))

    summary = [[escape(str(k)) for k, _ in report["summary"]], [escape(str(v)) for _, v in report["summary"]]]
    st = Table(summary)
    st.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef3f7")),
                            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8),
                            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")), ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    flow += [st, Spacer(1, 10)]

    data = [report["columns"]] + [[Paragraph(escape(str(c)), cell) for c in row] for row in report["rows"]]
    if len(data) == 1:
        data.append(["No attendance recorded for this selection."] + [""] * (len(report["columns"]) - 1))
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BRAND), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f7fa")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    flow.append(table)
    footer = _footer_factory(settings["school_name"])
    doc.build(flow, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
