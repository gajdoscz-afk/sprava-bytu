# Správa bytů – backend

Server pro správu nájmů: byty, nájemníci, předpisy záloh, vyúčtování služeb, historie změn.

## Spuštění na počítači

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ALLOW_NO_AUTH=1             # jen pro vývoj, bez přihlášení
python seed.py                     # naplní byt Sibiřská 375/12
uvicorn app.main:app --reload
```

Dokumentace API je na http://127.0.0.1:8000/docs.

## Testy

```bash
pytest -q
```

## Nasazení (GitHub + Render)

1. Vytvořte na GitHubu **soukromý** repozitář a nahrajte do něj tento projekt.
2. Na Renderu zvolte **New → Blueprint** a vyberte repozitář. Soubor `render.yaml` vytvoří web službu a databázi PostgreSQL ve Frankfurtu.
3. V nastavení služby doplňte `BASIC_USER` a `BASIC_PASS`. Bez nich server odmítne požadavky, takže se nikdo cizí nedostane k datům.
4. Po nasazení spusťte jednou seed na serveru (Render → Shell): `python seed.py`.

Ceny a názvy tarifů na Renderu se mění. Před založením zkontrolujte aktuální nabídku.

## Hlavní endpointy

| Metoda | Cesta | Co dělá |
|---|---|---|
| GET/POST | `/flats` | seznam bytů, nový byt |
| GET/PUT/DELETE | `/flats/{id}` | detail, úprava, smazání bytu |
| POST | `/flats/{id}/leases` | nový předpis (nájem a zálohy) |
| POST | `/flats/{id}/statements/{rok}/compare` | porovná položky z dokumentu, nic nezapisuje |
| POST | `/flats/{id}/statements/{rok}/resolve` | zapíše položky; u změn vyžaduje rozhodnutí `replace` nebo `keep` |
| GET | `/versions` | historie změn |
| POST | `/versions/{id}/restore` | vrátí starší hodnotu |
| GET | `/export` | celá data jako JSON (záloha) |

## Co dál

- Čtení fotek a PDF (OCR nebo model) jako samostatná služba, která vrací položky do `/compare`.
- Frontend: nahradit prototyp HTML stránkou, která volá tuto API.
- Plánované úlohy: upomínky a připomínky konce smluv.
- Notifikace e-mailem a do telefonu.
- Alembic pro migrace schématu, až budou první změny po ostrém provozu.
