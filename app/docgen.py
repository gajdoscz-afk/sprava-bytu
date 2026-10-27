"""Word dokumenty (.docx): smlouvy, dodatky a vyúčtování."""
from io import BytesIO

from docx import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def money(v) -> str:
    return f"{round(float(v or 0)):,}".replace(",", " ") + " Kč"


def _new(title: str) -> DocxDocument:
    d = DocxDocument()
    d.styles["Normal"].font.name = "Calibri"
    d.styles["Normal"].font.size = Pt(11)
    d.add_heading(title, level=1)
    return d


def _bold(cell) -> None:
    for p in cell.paragraphs:
        for r in p.runs:
            r.bold = True


def _table(d, headers, rows, money_cols=()):
    t = d.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    for i, h in enumerate(headers):
        t.rows[0].cells[i].text = h
        _bold(t.rows[0].cells[i])
    for row in rows:
        cells = t.add_row().cells
        bold = bool(row and str(row[0]).startswith("Celkem"))
        for i, v in enumerate(row):
            cells[i].text = str(v)
            if bold:
                _bold(cells[i])
            if i in money_cols:
                cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
    d.add_paragraph()
    return t


def _save(d) -> bytes:
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def text_docx(title: str, text: str) -> bytes:
    """Smlouva nebo dodatek z textu. Řádky „Čl. …“ jsou tučně, prázdný řádek odděluje odstavce."""
    d = _new(title)
    for line in text.split("\n"):
        if not line.strip():
            continue
        p = d.add_paragraph()
        r = p.add_run(line)
        if line.startswith("Čl. "):
            r.bold = True
    return _save(d)


def _cz_date(d) -> str:
    return f"{d.day}. {d.month}. {d.year}"


def settlement_tenant_docx(flat, res: dict, doc_date=None) -> bytes:
    """Vyúčtování služeb pro nájemníka: služby podle vyúčtování SVJ proti zálohám předepsaným podle předpisu."""
    from datetime import date as _date
    from . import landlord as _landlord
    y = res["year"]
    doc_date = doc_date or _date.today()
    land = _landlord.LANDLORD
    tenants = [flat.tenant_name] + [t.strip() for t in (flat.tenant_others or "").split(",") if t.strip()]
    d = _new(f"Vyúčtování služeb za rok {y}")
    d.add_paragraph(f"Období: 1. 1. {y} až 31. 12. {y}")
    d.add_paragraph(f"Pronajímatel: {land['name']}, {land['address']}, tel. {land['phone']}, {land['email']}")
    d.add_paragraph(f"Nájemce: {', '.join(t for t in tenants if t) or '[doplňte]'}")
    d.add_paragraph(f"Byt: {flat.addr}, {flat.zip or ''}, {flat.dispo or ''}".replace(", ,", ",").strip(", "))
    d.add_heading("Služby podle vyúčtování SVJ (přeúčtovatelné)", level=2)
    _table(d, ["Položka", "Náklad za rok"],
           [[i["name"], money(i["cost"])] for i in res["billable"]]
           + [["Celkem náklady na služby", money(res["billable_cost"])]], money_cols=(1,))
    d.add_heading("Zálohy nájemníka podle předpisu", level=2)
    _table(d, ["Položka", "Za rok"],
           [[k, money(v)] for k, v in res["advances"].items()]
           + [["Celkem zálohy", money(res["advances_total"])]], money_cols=(1,))
    bal = res["tenant_balance"]
    if bal > 0:
        verdict = f"Nedoplatek nájemníka: {money(bal)}"
    elif bal < 0:
        verdict = f"Přeplatek nájemníka: {money(-bal)}"
    else:
        verdict = "Vyúčtování vychází vyrovnané."
    d.add_heading("Výsledek", level=2)
    d.add_paragraph(verdict)
    d.add_paragraph(f"Přeplatek se vrací, nedoplatek se hradí na účet {flat.bank_acc or '[doplňte]'}.")
    d.add_paragraph()
    d.add_paragraph(f"V Praze dne {_cz_date(doc_date)}")
    d.add_paragraph()
    d.add_paragraph("______________________          ______________________")
    d.add_paragraph("Pronajímatel                     Nájemce")
    return _save(d)


def settlement_landlord_docx(flat, res: dict) -> bytes:
    y = res["year"]
    d = _new(f"Přehled vyúčtování za rok {y} pro pronajímatele")
    d.add_paragraph(f"Platnost přehledu: období od 1. 1. {y} do 31. 12. {y}")
    d.add_paragraph(f"Byt: {flat.addr}. Pouze pro vnitřní použití, nenahrávat nájemníkovi.")
    d.add_heading("Položky vyúčtování SVJ a správy", level=2)
    rows = []
    for i in res["items"]:
        rows.append([i["name"], money(i["cost"]), money(i["paid"]),
                     money(i["paid"] - i["cost"]),
                     "ano" if i["billable"] else "ne"])
    rows.append(["Celkem", money(res["landlord_cost"]), money(res["landlord_paid"]),
                 money(res["landlord_balance"]), ""])
    _table(d, ["Položka", "Náklad", "Zaplaceno", "Rozdíl", "Do nájemníka"], rows, money_cols=(1, 2, 3))
    d.add_paragraph("Zálohy SVJ jsou podle položek z vyúčtování SVJ. Platby nájemníka se v tomto přehledu nepoužívají.")
    return _save(d)
