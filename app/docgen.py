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


def settlement_tenant_docx(flat, res: dict, doc_date=None, signature: bytes | None = None) -> bytes:
    """Vyúčtování služeb pro nájemce: smluvní strany, položky, výpočet a výsledek."""
    from datetime import date as _date
    from . import landlord as _landlord
    y = res["year"]
    doc_date = doc_date or _date.today()
    land = _landlord.LANDLORD
    tenants = [flat.tenant_name] + [t.strip() for t in (flat.tenant_others or "").split(",") if t.strip()]
    tenant_txt = ", ".join(t for t in tenants if t) or "[doplňte]"
    d = _new(f"Vyúčtování služeb za rok {y}")
    d.add_paragraph(f"Vyúčtování služeb za období 1. 1. {y} až 31. 12. {y}. Předloženo dne {_cz_date(doc_date)}.")
    d.add_heading("Smluvní strany", level=2)
    _table(d, ["Pronajímatel", "Nájemce"], [[
        f"{land['name']}\n{land['address']}\ntel. {land['phone']}\n{land['email']}",
        f"{tenant_txt}\n{flat.addr}, {flat.zip or ''}\ntel. {flat.tenant_phone or '—'}\n{flat.tenant_email or ''}",
    ]])
    d.add_paragraph(f"Předmět: byt {flat.addr}, {flat.zip or ''}, {flat.dispo or ''}".replace(", ,", ",").replace(", ", ", ").rstrip(", "))
    d.add_heading("1. Náklady na přeúčtovatelné služby podle vyúčtování SVJ", level=2)
    _table(d, ["Položka", "Náklad za rok"],
           [[i["name"], money(i["cost"])] for i in res["billable"]]
           + [["Celkem náklady", money(res["billable_cost"])]], money_cols=(1,))
    d.add_heading("2. Zálohy nájemce podle předpisu", level=2)
    _table(d, ["Položka", "Za rok"],
           [[k, money(v)] for k, v in res["advances"].items()]
           + [["Celkem zálohy", money(res["advances_total"])]], money_cols=(1,))
    cost, adv, bal = res["billable_cost"], res["advances_total"], res["tenant_balance"]
    d.add_heading("3. Výpočet", level=2)
    d.add_paragraph(f"Náklady celkem {money(cost)} − zálohy celkem {money(adv)} = {money(bal)}")
    if bal > 0:
        verdict = f"Nedoplatek nájemce: {money(bal)}. Nájemce tuto částku doplatí pronajímateli."
    elif bal < 0:
        verdict = f"Přeplatek nájemce: {money(-bal)}. Pronajímatel tuto částku vrátí nájemci."
    else:
        verdict = "Vyúčtování vychází vyrovnané. Nevzniká přeplatek ani nedoplatek."
    d.add_heading("4. Výsledek", level=2)
    d.add_paragraph(verdict)
    d.add_paragraph("Přeplatky a nedoplatky si smluvní strany posílají na účty definované v nájemní smlouvě "
                    "do 30 dnů od předložení tohoto vyúčtování.")
    d.add_paragraph()
    d.add_paragraph(f"V Praze dne {_cz_date(doc_date)}")
    d.add_paragraph()
    import io
    from docx.shared import Cm
    sig_tbl = d.add_table(rows=1, cols=2)
    left, right = sig_tbl.rows[0].cells
    if signature:
        left.paragraphs[0].add_run().add_picture(io.BytesIO(signature), width=Cm(4.5))
    left.add_paragraph("______________________")
    right.paragraphs[0].add_run("")
    right.add_paragraph("______________________")
    left.add_paragraph(f"Pronajímatel: {land['name']}")
    right.add_paragraph(f"Nájemce: {tenant_txt}")
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
    d.add_heading("Kontrola: zálohy SVJ podle vašich předpisů", level=2)
    months = res.get("svj_predpis_by_month") or {}
    names = ["leden", "únor", "březen", "duben", "květen", "červen", "červenec", "srpen", "září", "říjen", "listopad", "prosinec"]
    rows = [[names[int(k) - 1], money(v)] for k, v in sorted(months.items(), key=lambda kv: int(kv[0]))]
    rows.append(["Celkem podle předpisů", money(res.get("svj_predpis_total", 0))])
    rows.append(["Zaplaceno podle vyúčtování SVJ", money(res["landlord_paid"])])
    rows.append(["Rozdíl (předpisy − vyúčtování SVJ)", money(res.get("svj_predpis_total", 0) - res["landlord_paid"])])
    _table(d, ["Měsíc / součet", "Částka"], rows, money_cols=(1,))
    return _save(d)
