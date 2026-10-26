"""PDF z Word dokumentů čistě v Pythonu (reportlab). Nevyžaduje LibreOffice.

Čte odstavce a tabulky z .docx a skládá z nich PDF s českým písmem DejaVu Sans.
"""
from io import BytesIO
from pathlib import Path

from docx import Document as DocxDocument
from docx.oxml.ns import qn
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, Paragraph, PageBreak, SimpleDocTemplate, Spacer, Table, TableStyle

FONTS = Path(__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("DejaVu", str(FONTS / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(FONTS / "DejaVuSans-Bold.ttf")))

BASE = ParagraphStyle("base", fontName="DejaVu", fontSize=10, leading=14, spaceAfter=4)
HEAD = ParagraphStyle("head", parent=BASE, fontName="DejaVu-Bold", fontSize=13, leading=17, spaceBefore=8, spaceAfter=6)
BOLD = ParagraphStyle("bold", parent=BASE, fontName="DejaVu-Bold")
CELL = ParagraphStyle("cell", parent=BASE, fontSize=9, leading=12, spaceAfter=0)
CELL_R = ParagraphStyle("cellr", parent=CELL, alignment=TA_RIGHT)


class PdfError(Exception):
    pass


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


EMU_PER_PT = 12700
FRAME_W = 170 * mm  # šířka textu na A4 se zachovanými okraji


def _images(element, part, max_w):
    """Obrázky (např. podpisy z displeje) z odstavce nebo buňky. Vrací flowables Image."""
    out = []
    for blip in element.xpath(".//a:blip"):
        rid = blip.get(qn("r:embed"))
        if not rid:
            continue
        try:
            data = part.related_parts[rid].blob
        except KeyError:
            continue
        ext = blip.xpath("ancestor::wp:inline/wp:extent | ancestor::wp:anchor/wp:extent")
        cx = int(ext[0].get("cx")) / EMU_PER_PT if ext else 120.0
        cy = int(ext[0].get("cy")) / EMU_PER_PT if ext else 60.0
        w = min(cx, max_w)
        h = cy * (w / cx) if cx else cy
        try:
            out.append(Image(BytesIO(data), width=w, height=h))
        except Exception:
            continue
    return out


def _para_flow(p, out):
    if any(r._r.find(qn("w:br")) is not None and r._r.find(qn("w:br")).get(qn("w:type")) == "page"
           for r in p.runs):
        out.append(PageBreak())
        return
    text = p.text
    imgs = _images(p._p, p.part, FRAME_W)
    if not text.strip() and not imgs:
        out.append(Spacer(1, 4))
        return
    style_name = (p.style.name or "").lower() if p.style is not None else ""
    if text.strip():
        if style_name.startswith("heading") or style_name == "title":
            out.append(Paragraph(_esc(text), HEAD))
        elif p.runs and all(r.bold for r in p.runs if r.text.strip()):
            out.append(Paragraph(_esc(text), BOLD))
        else:
            out.append(Paragraph(_esc(text), BASE))
    out.extend(imgs)


def _table_flow(t, out, width):
    rows = []
    for row in t.rows:
        cells = []
        seen = set()
        for c in row.cells:
            if id(c._tc) in seen:  # sloučené buňky se vypíšou jen jednou
                continue
            seen.add(id(c._tc))
            content = [Paragraph(_esc(c.text), CELL)] if c.text.strip() else []
            for cp in c.paragraphs:
                content.extend(_images(cp._p, cp.part, width / max(1, len(row.cells)) - 8))
            cells.append(content or [Paragraph("", CELL)])
        rows.append(cells)
    if not rows:
        return
    ncols = max(len(r) for r in rows)
    rows = [r + [[Paragraph("", CELL)]] * (ncols - len(r)) for r in rows]
    tbl = Table(rows, colWidths=[width / ncols] * ncols, repeatRows=0)
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    out.append(tbl)
    out.append(Spacer(1, 8))


def docx_to_pdf(data: bytes) -> bytes:
    try:
        doc = DocxDocument(BytesIO(data))
    except Exception as e:  # poškozený soubor
        raise PdfError(f"Dokument nejde načíst: {e}")
    from docx.table import Table as DocxTable
    from docx.text.paragraph import Paragraph as DocxParagraph

    story = []
    width = A4[0] - 40 * mm
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            _para_flow(DocxParagraph(child, doc), story)
        elif child.tag == qn("w:tbl"):
            _table_flow(DocxTable(child, doc), story, width)
    if not story:
        raise PdfError("Dokument je prázdný")
    out = BytesIO()
    SimpleDocTemplate(out, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                      topMargin=18 * mm, bottomMargin=18 * mm,
                      title="Dokument").build(story)
    return out.getvalue()
