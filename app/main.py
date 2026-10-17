import os
import secrets
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from . import schemas as s
from .db import Base, engine, get_db

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


# ---------- záloha ----------

@app.get("/export")
def export_all(db: Session = Depends(get_db)):
    flats = db.scalars(select(m.Flat).order_by(m.Flat.addr)).all()
    return {"flats": [flat_out(f) for f in flats]}
