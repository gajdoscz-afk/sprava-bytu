import os

os.environ["DATABASE_URL"] = "sqlite:///./test.db"
os.environ["ALLOW_NO_AUTH"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def make_flat():
    r = client.post("/flats", json={"addr": "Testovací 1/2", "tenant": {"name": "Jan Test", "others": []}})
    assert r.status_code == 201
    return r.json()["id"]


def test_create_and_read_flat():
    fid = make_flat()
    r = client.get(f"/flats/{fid}")
    assert r.status_code == 200
    assert r.json()["tenant"]["name"] == "Jan Test"


def test_new_statement_then_conflict_needs_decision():
    fid = make_flat()
    first = {"doc_label": "2025", "items": [{"name": "Teplo", "cost": "1000", "paid": "900"}]}
    assert client.post(f"/flats/{fid}/statements/2025/resolve", json=first).status_code == 200

    changed = {"items": [{"name": "Teplo", "cost": "1200", "paid": "900"}]}
    r = client.post(f"/flats/{fid}/statements/2025/resolve", json=changed)
    assert r.status_code == 409  # bez rozhodnutí se nic nepřepíše
    assert r.json()["detail"]["undecided"] == ["Teplo"]


def test_replace_keeps_old_value_and_restore_works():
    fid = make_flat()
    client.post(f"/flats/{fid}/statements/2025/resolve",
                json={"items": [{"name": "Teplo", "cost": "1000", "paid": "900"}]})
    r = client.post(f"/flats/{fid}/statements/2025/resolve",
                    json={"items": [{"name": "Teplo", "cost": "1200", "paid": "900"}],
                          "decisions": {"Teplo": "replace"}})
    assert r.status_code == 200
    st = client.get(f"/flats/{fid}").json()["statements"][0]
    assert st["items"][0]["cost"] == 1200.0

    versions = client.get(f"/versions?flat_id={fid}").json()
    replaced = next(v for v in versions if v["action"] == "nahrazeno")
    assert replaced["old"]["cost"] == 1000.0

    assert client.post(f"/versions/{replaced['id']}/restore").status_code == 200
    st = client.get(f"/flats/{fid}").json()["statements"][0]
    assert st["items"][0]["cost"] == 1000.0


def test_keep_does_not_change_value():
    fid = make_flat()
    client.post(f"/flats/{fid}/statements/2025/resolve",
                json={"items": [{"name": "Teplo", "cost": "1000", "paid": "900"}]})
    client.post(f"/flats/{fid}/statements/2025/resolve",
                json={"items": [{"name": "Teplo", "cost": "1500", "paid": "900"}],
                      "decisions": {"Teplo": "keep"}})
    st = client.get(f"/flats/{fid}").json()["statements"][0]
    assert st["items"][0]["cost"] == 1000.0


def test_compare_does_not_write():
    fid = make_flat()
    r = client.post(f"/flats/{fid}/statements/2025/compare",
                    json={"items": [{"name": "Teplo", "cost": "1000", "paid": "0"}]})
    assert r.json()["diffs"][0]["status"] == "new"
    assert client.get(f"/flats/{fid}").json()["statements"] == []


def test_balance_sign():
    fid = make_flat()
    client.post(f"/flats/{fid}/statements/2025/resolve",
                json={"items": [{"name": "Teplo", "cost": "1000", "paid": "600"}]})
    st = client.get(f"/flats/{fid}").json()["statements"][0]
    assert st["balance"] == -400.0  # nedoplatek
