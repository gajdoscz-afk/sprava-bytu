"""Naplní databázi daty z podkladů (Sibiřská 375/12). Spusťte jednou: python seed.py

Respektuje DATABASE_URL. Pokud byt už existuje, nic nedělá.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app import models as m
from app.db import Base, SessionLocal, engine

ITEMS_2023 = [
    ("Výtah", "2132.06", "1200"), ("Osvětlení", "502.10", "864"), ("Prádelna, mandl", "0", "0"),
    ("Správa vodoměrů", "455.32", "420"), ("Správa domu", "1818.63", "1224"), ("Úklid", "898.71", "864"),
    ("Studená voda", "11334.50", "8136"), ("Teplá voda", "22996.40", "7992"), ("Teplo", "20790.20", "14556"),
]
ITEMS_2025 = [
    ("Výtah", "2532.01", "1200"), ("Osvětlení", "607.11", "864"), ("Prádelna, mandl", "0", "0"),
    ("Správa vodoměrů", "443.44", "420"), ("Správa domu", "1873.08", "1224"), ("Úklid", "985.32", "864"),
    ("Studená voda", "11166.60", "8136"), ("Teplá voda", "28125.50", "7992"), ("Teplo", "13476.10", "14556"),
]


def main():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        if db.scalar(select(m.Flat).where(m.Flat.addr == "Sibiřská 375/12")):
            print("Byt už existuje, nic se nemění.")
            return
        f = m.Flat(
            addr="Sibiřská 375/12", dispo="2+1", floor="5. NP", zip="400 07 Ústí nad Labem",
            owner="Michal Gajdoš", bank_acc="2238724016/3030",
            tenant_name="Karolína Ferencová", tenant_email="", tenant_phone="735 957 153",
            tenant_others="Sylvie Ferencová",
        )
        f.leases.append(m.Lease(
            valid_from=date(2023, 1, 1), valid_to=date(2023, 12, 31),
            rent=Decimal("9475"), deposit=Decimal("20000"),
            advances=[m.Advance(name=k, amount=Decimal(str(v))) for k, v in {
                "Studená voda a stočné": 720, "Teplá voda": 840, "Vytápění": 1455,
                "Výtah": 130, "Osvětlení": 80, "Úklid": 100}.items()],
        ))
        for year, items, label in [
            (2023, ITEMS_2023, "Vyúčtování SVJ, vydáno 27. 3. 2024"),
            (2025, ITEMS_2025, "Vyúčtování SVJ + Ulimex, vydáno 17. 3. 2026"),
        ]:
            st = m.Statement(year=year, doc_label=label)
            st.items = [m.StatementItem(name=n, cost=Decimal(c), paid=Decimal(p), position=i)
                        for i, (n, c, p) in enumerate(items)]
            f.statements.append(st)
        db.add(f)
        db.commit()
        print("Byt Sibiřská 375/12 založen.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
