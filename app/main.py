import os
import secrets
from contextlib import asynccontextmanager
from decimal import Decimal

from pathlib import Path

import mimetypes

from fastapi import Depends, FastAPI, File, HTTPException, Response, UploadFile, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import documents
from . import models as m
from . import schemas as s
from .db import Base, engine, get_db

MAX_UPLOAD_BYTES = 15 * 1024 * 1024

security = HTTPBasic()


def require_auth(creds: HTTPBasicCredentials = Depends(security)) -> None:
    """Jeden uživatel, přihlášení přes BASIC_USER a BASIC_PASS. Bez nich se server nespustí."""
    user, pwd = os.getenv("BASIC_USER"), os.getenv("BASIC_PASS")
    if not user or not pwd:
        if os.getenv("ALLOW_NO_AUTH") == "1":  # jen pro lokální vývoj a testy
            return
        raise HTTPException(500, "Na serveru chybí BASIC_USER a BASIC_PASS")
    ok = secrets.compare_digest(creds.username, user) and secrets.compare_digest(creds.password, pwd)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Neplatné přihlášení", {"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # MVP: tabulky vytvoříme při startu. Až budou změny schématu, přejdeme na Alembic.
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Správa bytů", version="0.3.0", lifespan=lifespan, dependencies=[Depends(require_auth)])


# ---------- pomocné funkce ----------

def money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


def get_flat(db: Session, flat_id: int) -> m.Flat:
    f = db.get(m.Flat, flat_id)
    if f is None:
        raise HTTPException(404, "Byt nenalezen")
    return f


def get_statement(db: Session, flat_id: int, year: int) -> m.Statement | None:
    return db.scalar(select(m.Statement).where(m.Statement.flat_id == flat_id, m.Statement.year == year))


def same(item: m.StatementItem, new: s.ItemIn) -> bool:
    return money(item.cost) == money(new.cost) and money(item.paid) == money(new.paid)


def item_dict(item) -> dict:
    return {"cost": float(item.cost), "paid": float(item.paid)}


def lease_out(l: m.Lease) -> dict:
    return {
        "id": l.id,
        "valid_from": l.valid_from.isoformat(),
        "valid_to": l.valid_to.isoformat() if l.valid_to else None,
        "rent": float(l.rent),
        "deposit": float(l.deposit),
        "advances": {a.name: float(a.amount) for a in l.advances},
    }


def statement_out(st: m.Statement) -> dict:
    cost = sum((i.cost for i in st.items), Decimal("0"))
    paid = sum((i.paid for i in st.items), Decimal("0"))
    return {
        "year": st.year,
        "doc_label": st.doc_label,
        "items": [{"id": i.id, "name": i.name, "cost": float(i.cost), "paid": float(i.paid)} for i in st.items],
        "balance": float(paid - cost),  # kladné = přeplatek, záporné = nedoplatek
    }


def flat_out(f: m.Flat) -> dict:
    return {
        "id": f.id,
        "addr": f.addr,
        "dispo": f.dispo,
        "floor": f.floor,
        "zip": f.zip,
        "owner": f.owner,
        "bank_acc": f.bank_acc,
        "tenant": {
            "name": f.tenant_name,
            "email": f.tenant_email,
            "phone": f.tenant_phone,
            "others": [x.strip() for x in f.tenant_others.split(",") if x.strip()],
        },
        "leases": [lease_out(l) for l in f.leases],
        "statements": [statement_out(st) for st in f.statements],
    }


def apply_flat(f: m.Flat, data: s.FlatIn) -> None:
    f.addr = data.addr
    f.dispo, f.floor, f.zip, f.owner, f.bank_acc = data.dispo, data.floor, data.zip, data.owner, data.bank_acc
    f.tenant_name = data.tenant.name
    f.tenant_email = data.tenant.email
    f.tenant_phone = data.tenant.phone
    f.tenant_others = ",".join(x.strip() for x in data.tenant.others if x.strip())


# ---------- byty ----------

@app.get("/flats")
def list_flats(db: Session = Depends(get_db)):
    flats = db.scalars(select(m.Flat).order_by(m.Flat.addr)).all()
    return [flat_out(f) for f in flats]


@app.post("/flats", status_code=201)
def create_flat(data: s.FlatIn, db: Session = Depends(get_db)):
    f = m.Flat()
    apply_flat(f, data)
    db.add(f)
    db.commit()
    return flat_out(f)


@app.get("/flats/{flat_id}")
def read_flat(flat_id: int, db: Session = Depends(get_db)):
    return flat_out(get_flat(db, flat_id))


@app.put("/flats/{flat_id}")
def update_flat(flat_id: int, data: s.FlatIn, db: Session = Depends(get_db)):
    f = get_flat(db, flat_id)
    apply_flat(f, data)
    db.commit()
    return flat_out(f)


@app.delete("/flats/{flat_id}", status_code=204)
def delete_flat(flat_id: int, db: Session = Depends(get_db)):
    db.delete(get_flat(db, flat_id))
    db.commit()
    return Response(status_code=204)


# ---------- předpisy ----------

@app.post("/flats/{flat_id}/leases", status_code=201)
def add_lease(flat_id: int, data: s.LeaseIn, db: Session = Depends(get_db)):
    f = get_flat(db, flat_id)
    lease = m.Lease(
        flat=f, valid_from=data.valid_from, valid_to=data.valid_to,
        rent=money(data.rent), deposit=money(data.deposit),
        advances=[m.Advance(name=k, amount=money(v)) for k, v in data.advances.items()],
    )
    db.add(lease)
    db.add(m.Version(
        flat_id=f.id, name=f"Předpis {data.valid_from.isoformat()} až {data.valid_to.isoformat() if data.valid_to else 'bez konce'}",
        action="uložen", new={"rent": float(lease.rent), "advances": {k: float(v) for k, v in data.advances.items()}},
    ))
    db.commit()
    return lease_out(lease)


# ---------- vyúčtování: porovnání a rozhodnutí ----------

@app.post("/flats/{flat_id}/statements/{year}/compare")
def compare_statement(flat_id: int, year: int, body: s.ExtractedIn, db: Session = Depends(get_db)):
    """Porovná položky z dokumentu s uloženými. Nic nezapisuje."""
    get_flat(db, flat_id)
    st = get_statement(db, flat_id, year)
    stored = {i.name: i for i in st.items} if st else {}
    diffs = []
    for it in body.items:
        old = stored.get(it.name)
        if old is None:
            diffs.append({"name": it.name, "status": "new", "stored": None, "extracted": item_dict(it)})
        elif not same(old, it):
            diffs.append({"name": it.name, "status": "changed", "stored": item_dict(old), "extracted": item_dict(it)})
    return {"year": year, "exists": st is not None, "diffs": diffs, "unchanged": len(body.items) - len(diffs)}


@app.post("/flats/{flat_id}/statements/{year}/resolve")
def resolve_statement(flat_id: int, year: int, body: s.ResolveIn, db: Session = Depends(get_db)):
    """Zapíše položky. U každé změněné položky musí být rozhodnutí: replace, nebo keep."""
    f = get_flat(db, flat_id)
    st = get_statement(db, flat_id, year)
    if st is None:
        st = m.Statement(flat=f, year=year, doc_label=body.doc_label)
        db.add(st)
        db.flush()
    stored = {i.name: i for i in st.items}

    undecided = [
        it.name for it in body.items
        if it.name in stored and not same(stored[it.name], it) and it.name not in body.decisions
    ]
    if undecided:
        raise HTTPException(409, {"message": "Některé rozdíly nemají rozhodnutí", "undecided": undecided})

    results = []
    for it in body.items:
        old = stored.get(it.name)
        if old is None:
            item = m.StatementItem(name=it.name, cost=money(it.cost), paid=money(it.paid), position=len(st.items))
            st.items.append(item)
            stored[it.name] = item
            db.add(m.Version(flat_id=f.id, statement_id=st.id, name=it.name, action="přidáno",
                             new=item_dict(item)))
            results.append({"name": it.name, "result": "přidáno"})
        elif same(old, it):
            continue
        elif body.decisions[it.name] == "replace":
            db.add(m.Version(flat_id=f.id, statement_id=st.id, item_id=old.id, name=it.name, action="nahrazeno",
                             old=item_dict(old), new=item_dict(it)))
            old.cost, old.paid = money(it.cost), money(it.paid)
            results.append({"name": it.name, "result": "nahrazeno"})
        else:
            db.add(m.Version(flat_id=f.id, statement_id=st.id, item_id=old.id, name=it.name, action="ponecháno",
                             old=item_dict(old), new=item_dict(it)))
            results.append({"name": it.name, "result": "ponecháno"})
    db.commit()
    return {"applied": results}


# ---------- historie ----------

@app.get("/versions")
def list_versions(flat_id: int | None = None, db: Session = Depends(get_db)):
    q = select(m.Version).order_by(m.Version.id.desc()).limit(300)
    if flat_id is not None:
        q = q.where(m.Version.flat_id == flat_id)
    return [
        {"id": v.id, "flat_id": v.flat_id, "name": v.name, "action": v.action,
         "old": v.old, "new": v.new, "created_at": v.created_at.isoformat()}
        for v in db.scalars(q)
    ]


@app.post("/versions/{version_id}/restore")
def restore_version(version_id: int, db: Session = Depends(get_db)):
    v = db.get(m.Version, version_id)
    if v is None or v.action != "nahrazeno" or v.item_id is None or v.old is None:
        raise HTTPException(400, "Tuto změnu nejde vrátit")
    item = db.get(m.StatementItem, v.item_id)
    if item is None:
        raise HTTPException(404, "Položka už neexistuje")
    current = item_dict(item)
    item.cost, item.paid = money(v.old["cost"]), money(v.old["paid"])
    db.add(m.Version(flat_id=v.flat_id, statement_id=v.statement_id, item_id=item.id, name=v.name,
                     action="vráceno", old=current, new=v.old))
    db.commit()
    return {"ok": True}


# ---------- dokumenty ----------

def get_document(db: Session, doc_id: int) -> m.Document:
    d = db.get(m.Document, doc_id)
    if d is None:
        raise HTTPException(404, "Dokument nenalezen")
    return d


def doc_out(d: m.Document) -> dict:
    return {
        "id": d.id, "flat_id": d.flat_id, "status": d.status, "doc_type": d.doc_type,
        "filename": d.filename, "mime": d.mime, "size": d.size, "year": d.year,
        "summary": d.summary, "note": d.note, "suggested_flat_id": d.suggested_flat_id,
        "extracted": d.extracted, "created_at": d.created_at.isoformat(),
    }


def run_classification(d: m.Document, db: Session) -> None:
    """Přečte dokument a nastaví typ, rok, shrnutí a navržený byt. Automaticky zařadí jen při jisté shodě."""
    flats = db.scalars(select(m.Flat)).all()
    aliases = db.scalars(select(m.FlatAlias)).all()
    try:
        result = documents.classify(d.data, d.mime)
    except documents.ClassifyError as e:
        d.note = str(e)
        d.status = "ke_kontrole" if d.flat_id is None else d.status
        return

    d.extracted = result
    d.doc_type = result.get("doc_type", "jine")
    try:
        d.year = int(result["year"]) if result.get("year") else None
    except (TypeError, ValueError):
        d.year = None
    d.summary = str(result.get("summary") or "")[:500]
    d.note = ""
    d.suggested_flat_id = documents.suggest_flat(result, flats, aliases)

    if d.flat_id is None and d.suggested_flat_id and result["confidence"] >= documents.AUTO_FILE_CONFIDENCE:
        d.flat_id = d.suggested_flat_id
        d.status = "zarazeno"
    elif d.flat_id is None:
        d.status = "ke_kontrole"


@app.post("/documents", status_code=201)
async def upload_documents(files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    out = []
    for up in files:
        data = await up.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{up.filename}: soubor je větší než 15 MB")
        mime = up.content_type or mimetypes.guess_type(up.filename or "")[0] or "application/octet-stream"
        d = m.Document(filename=(up.filename or "dokument")[:300], mime=mime, size=len(data), data=data)
        db.add(d)
        db.flush()
        await run_in_threadpool(run_classification, d, db)
        db.commit()
        out.append(doc_out(d))
    return out


@app.get("/documents")
def list_documents(flat_id: int | None = None, db: Session = Depends(get_db)):
    q = select(m.Document).order_by(m.Document.id.desc())
    if flat_id is not None:
        q = q.where(m.Document.flat_id == flat_id)
    return [doc_out(d) for d in db.scalars(q)]


@app.get("/documents/{doc_id}/file")
def document_file(doc_id: int, db: Session = Depends(get_db)):
    d = get_document(db, doc_id)
    return Response(content=d.data, media_type=d.mime, headers={"Content-Disposition": "inline"})


@app.patch("/documents/{doc_id}")
def update_document(doc_id: int, body: s.DocumentUpdate, db: Session = Depends(get_db)):
    d = get_document(db, doc_id)
    if body.doc_type is not None:
        if body.doc_type not in documents.DOC_TYPES:
            raise HTTPException(400, "Neznámý typ dokumentu")
        d.doc_type = body.doc_type
    if "flat_id" in body.model_fields_set:
        if body.flat_id is not None:
            get_flat(db, body.flat_id)
        d.flat_id = body.flat_id
        d.status = "zarazeno" if body.flat_id is not None else "ke_kontrole"
        # Zapamatuje si, jak se adresa na dokumentu píše, aby příště byl byt poznán sám
        addr = documents.normalize((d.extracted or {}).get("address") or "")
        if body.flat_id is not None and addr:
            exists = db.scalar(select(m.FlatAlias).where(
                m.FlatAlias.flat_id == body.flat_id, m.FlatAlias.alias == addr[:200]))
            if exists is None:
                db.add(m.FlatAlias(flat_id=body.flat_id, alias=addr[:200]))
    db.commit()
    return doc_out(d)


@app.post("/documents/{doc_id}/classify")
async def reclassify_document(doc_id: int, db: Session = Depends(get_db)):
    d = get_document(db, doc_id)
    await run_in_threadpool(run_classification, d, db)
    db.commit()
    return doc_out(d)


@app.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: int, db: Session = Depends(get_db)):
    db.delete(get_document(db, doc_id))
    db.commit()
    return Response(status_code=204)


# ---------- záloha ----------

@app.get("/export")
def export_all(db: Session = Depends(get_db)):
    flats = db.scalars(select(m.Flat).order_by(m.Flat.addr)).all()
    return {"flats": [flat_out(f) for f in flats]}


# ---------- jednoduchá stránka pro správu (chráněná stejným přihlášením) ----------

@app.get("/", response_class=HTMLResponse)
def index(_: None = Depends(require_auth)):
    return HTMLResponse((Path(__file__).parent / "ui.html").read_text(encoding="utf-8"))
