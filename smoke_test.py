"""End-to-end smoke tests (run with a throwaway DB).

Covers:
  * intake validation (max 12, duplicate IDs)
  * pre-selection ranking (maximize limiting capacity, ID lex tie-break)
  * infeasibility reason codes
  * confirm happy path + immutable snapshot query
  * version-revision before confirm -> 409 VERSION_CONFLICT, nothing occupied
  * competing confirmation -> 409 OCCUPIED, winner takes all, no partial state
  * later revision never mutates confirmed snapshot
  * DB triggers reject UPDATE/DELETE on groups & snapshots
  * concurrent confirms of the same candidate (threads) -> exactly one winner
"""
import os
import sys
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ.setdefault("PYTHONPATH", "/workspace")

sys.path.insert(0, "/workspace")
# user-site where pip installed fastapi etc.
import site
site.addsitedir("/tmp/pyuser/lib/python3.11/site-packages")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.db import get_conn, init_db  # noqa: E402

init_db()
client = TestClient(app)

passed = 0


def check(name, cond, extra=""):
    global passed
    assert cond, f"FAIL: {name} {extra}"
    passed += 1
    print(f"  ok - {name}")


LFP = [
    ("C01", 5020, 1.05), ("C02", 5015, 1.08), ("C03", 5012, 1.10),
    ("C04", 5008, 1.12), ("C05", 5010, 0.95), ("C06", 5005, 1.20),
    ("C07", 5010, 1.00), ("C08", 4990, 1.02), ("C09", 4985, 1.30),
    ("C10", 5030, 1.50), ("C11", 4960, 0.90), ("C12", 4995, 1.06),
]

def bulk(cells, chem="LFP", ver="REV-1"):
    return client.post("/cells", json={"cells": [
        {"id": i, "chemistry": chem, "capacity_ah": c,
         "resistance_mohm": r, "test_version": ver}
        for i, c, r in cells]})

def pre(chem="LFP", mn=3, mx=6, cap=4980, dc=30, dr=0.25):
    return client.post("/preselect", json={
        "chemistry": chem, "min_size": mn, "max_size": mx,
        "min_capacity_ah": cap, "max_capacity_delta": dc,
        "max_resistance_delta": dr})

def confirm_body(ids, vers=None, chem="LFP", constraints=None):
    base = {"chemistry": chem, "candidate_ids": ids,
            "versions": vers or {i: "REV-1" for i in ids},
            "min_size": 3, "max_size": 6, "min_capacity_ah": 4980,
            "max_capacity_delta": 30, "max_resistance_delta": 0.25}
    if constraints:
        base.update(constraints)
    return base


print("1) intake validation")
r = bulk([("D1", 100, 1.0)] * 13)
check("max 12 cells -> 422", r.status_code == 422)
r = bulk([("D1", 100, 1.0), ("D1", 101, 1.0)])
check("duplicate IDs in batch -> 422", r.status_code == 422)
r = bulk(LFP)
check("12 cells accepted", r.status_code == 201 and len(r.json()) == 12)
r = bulk([("C01", 5000, 1.0)])
check("duplicate DB ID -> 409 DUPLICATE_CELL_ID",
      r.status_code == 409 and r.json()["detail"]["code"] == "DUPLICATE_CELL_ID")
r = client.post("/cells", json={"cells": [
    {"id": "X", "chemistry": "L", "capacity_ah": -1,
     "resistance_mohm": 0, "test_version": "v"}]})
check("non-positive capacity -> 422", r.status_code == 422)

print("2) pre-selection ranking")
r = pre()
j = r.json()
check("feasible", j["feasible"] is True)
check("picks C01,C02,C03 (limiting 5012 beats resistance-outlier C10)",
      j["candidate"]["candidate_ids"] == ["C01", "C02", "C03"],
      j["candidate"])
check("limiting capacity = 5012",
      j["candidate"]["limiting_capacity_ah"] == 5012)
check("versions returned per cell",
      j["candidate"]["versions"] == {"C01": "REV-1", "C02": "REV-1", "C03": "REV-1"})
check("pool size = all 12 unoccupied LFP cells (capacity filter is separate)",
      j["pool_size"] == 12, j["pool_size"])

print("3) tie-break: equal limiting capacity -> lexicographic IDs")
# Fresh DB scenario via a separate chemistry: two disjoint triples same min.
tb = [("A1", 100, 1.0), ("A2", 100, 1.0), ("A3", 100, 1.0),
      ("B1", 100, 1.0), ("B2", 100, 1.0), ("B3", 100, 1.0)]
r = bulk(tb, chem="TIE")
r = pre("TIE", cap=100, dc=1, dr=1)
check("lexicographically smallest tuple (A1,A2,A3)",
      r.json()["candidate"]["candidate_ids"] == ["A1", "A2", "A3"],
      r.json()["candidate"])

print("4) infeasible reasons")
r = pre("NMC")
check("unknown chemistry -> ALL_OCCUPIED",
      r.json()["reason_code"] == "ALL_OCCUPIED")
r = pre(cap=5100)
check("min capacity unreachable",
      r.json()["reason_code"] == "MIN_CAPACITY_UNREACHABLE", r.json())
# capacity spread impossible: tightest triple spans 2 Ah (5010/5010/5008)
r = pre(cap=4955, dc=1.5, dr=2)
check("capacity spread exceeded",
      r.json()["reason_code"] == "CAPACITY_SPREAD_EXCEEDED", r.json()["reason_code"])
r = pre(cap=4955, dc=1000, dr=0.01)
check("resistance spread exceeded",
      r.json()["reason_code"] == "RESISTANCE_SPREAD_EXCEEDED", r.json()["reason_code"])
# only one cell clears the capacity floor (C10 at 5030) -> fewer than min_size
r = pre(cap=5030, mn=3, mx=6, dc=1000, dr=1000)
check("not enough cells -> NOT_ENOUGH_CELLS",
      r.json()["reason_code"] == "NOT_ENOUGH_CELLS", r.json())
# Joint infeasibility, exact construction (triples, dc=1.5, dr=0.5):
#   cap-feasible triples live in the 100/101 cluster {A,B,C,D}; any triple of
#   these mixes resistance levels (spread >= 1.0);
#   res-feasible triples must be {A,E,F} at level 1.0, whose cap spread is 100.
joint = [("A", 100, 1.0), ("B", 100, 2.0), ("C", 101, 3.0),
         ("D", 101, 2.0), ("E", 150, 1.0), ("F", 200, 1.0)]
bulk(joint, chem="JOINT")
r = pre("JOINT", mn=3, mx=3, cap=100, dc=1.5, dr=0.5)
check("each spread feasible alone but not together -> CONSTRAINTS_INCOMPATIBLE",
      r.json()["feasible"] is False
      and r.json()["reason_code"] == "CONSTRAINTS_INCOMPATIBLE", r.json())
# sanity: relaxing either constraint alone yields a feasible group
check("relax resistance delta -> feasible",
      pre("JOINT", mn=3, mx=3, cap=100, dc=1.5, dr=3.0).json()["feasible"] is True)
check("relax capacity delta -> feasible",
      pre("JOINT", mn=3, mx=3, cap=100, dc=100, dr=0.5).json()["feasible"] is True)

print("5) confirm happy path (group 1)")
r = client.post("/confirm", json=confirm_body(["C01", "C02", "C03"]))
check("confirm 200 + group_no 1",
      r.status_code == 200 and r.json()["group_no"] == 1, r.text)
check("snapshot has 3 ordered cells",
      [c["cell_id"] for c in r.json()["snapshot_cells"]] == ["C01", "C02", "C03"])
r = client.get("/groups/1")
g = r.json()
check("GET /groups/1 returns frozen snapshot",
      [c["test_version"] for c in g["cells"]] == ["REV-1"] * 3
      and g["limiting_capacity_ah"] == 5012)
r = client.get("/groups/99")
check("unknown group -> 404", r.status_code == 404)
occ = {c["id"]: c["occupied"] for c in client.get(
    "/cells", params={"chemistry": "LFP"}).json()}
check("C01-C03 occupied, rest free",
      all(occ[i] for i in ["C01", "C02", "C03"])
      and not any(occ[i] for i in ["C04", "C05", "C07", "C10"]))

print("6) stale version -> 409, nothing occupied")
r = client.patch("/cells/C05/test-version", json={"test_version": "REV-2"})
check("revision accepted", r.status_code == 200 and r.json()["test_version"] == "REV-2")
r = client.post("/confirm", json=confirm_body(["C04", "C05", "C07"]))
check("VERSION_CONFLICT 409",
      r.status_code == 409 and r.json()["detail"]["code"] == "VERSION_CONFLICT",
      r.text)
check("conflict names C05",
      r.json()["detail"]["version_conflicts"][0]["cell_id"] == "C05")
free = {c["id"]: c["occupied"] for c in client.get("/cells").json()}
check("no partial occupancy after rejection",
      not any(free[i] for i in ["C04", "C05", "C07"]))

# re-preselect picks up new version
r = pre()
j = r.json()
check("re-preselect selects C04,C05,C07 with REV-2 for C05",
      j["candidate"]["candidate_ids"] == ["C04", "C05", "C07"]
      and j["candidate"]["versions"]["C05"] == "REV-2", j["candidate"])
r = client.post("/confirm", json=confirm_body(
    ["C04", "C05", "C07"],
    {"C04": "REV-1", "C05": "REV-2", "C07": "REV-1"}))
check("confirm with fresh versions -> group 2",
      r.status_code == 200 and r.json()["group_no"] == 2, r.text)

print("7) competing confirmation of the same cells -> OCCUPIED")
r = pre()
ids = r.json()["candidate"]["candidate_ids"]
vers = r.json()["candidate"]["versions"]
# first confirms
r1 = client.post("/confirm", json=confirm_body(ids, dict(vers)))
check("first confirm wins", r1.status_code == 200, r1.text)
# second tries the identical candidate -> occupied
r2 = client.post("/confirm", json=confirm_body(ids, dict(vers)))
check("second confirm -> 409 OCCUPIED",
      r2.status_code == 409 and r2.json()["detail"]["code"] == "OCCUPIED", r2.text)
state = {c["id"]: c["occupied_group"] for c in client.get("/cells").json()}
check("no partial occupancy: winner's cells share the new group, reject writes nothing",
      state[ids[0]] == r1.json()["group_no"]
      and all(state[i] == r1.json()["group_no"] for i in ids)
      and all(v not in (None, 1, 2) for v in [state[i] for i in ids]), state)

print("8) revision after confirmation must not mutate snapshot")
before = client.get("/groups/2").json()
client.patch("/cells/C04/test-version", json={"test_version": "REV-9"})
after = client.get("/groups/2").json()
check("group 2 snapshot unchanged", before == after)
check("live cell shows REV-9",
      client.get("/cells").json() and
      [c for c in client.get("/cells").json() if c["id"] == "C04"][0]["test_version"] == "REV-9")

print("9) storage-level immutability triggers")
conn = get_conn()
for stmt, label in [
    ("UPDATE groups SET limiting_capacity = 0 WHERE id = 1", "groups UPDATE"),
    ("DELETE FROM groups WHERE id = 1", "groups DELETE"),
    ("UPDATE group_snapshots SET test_version='X' WHERE group_id=1", "snapshot UPDATE"),
    ("DELETE FROM group_snapshots WHERE group_id=1", "snapshot DELETE"),
    ("UPDATE cells SET occupied_group=NULL WHERE id='C01'", "cell release"),
]:
    try:
        conn.execute(stmt)
        conn.commit()
        raised = False
    except Exception:
        raised = True
        conn.rollback()
    check(f"trigger blocks {label}", raised)
# but revising an occupied cell's version IS allowed
conn.execute("UPDATE cells SET test_version='REV-10' WHERE id='C01'")
conn.commit()
check("occupied cell version revision still allowed",
      conn.execute("SELECT test_version FROM cells WHERE id='C01'").fetchone()[0] == "REV-10")
# double booking via raw insert is impossible (UNIQUE cell_id)
try:
    conn.execute("INSERT INTO group_snapshots (group_id,cell_id,position,chemistry,"
                 "capacity_ah,resistance_mohm,test_version) VALUES (2,'C01',9,'LFP',1,1,'v')")
    conn.commit()
    raised = False
except Exception:
    raised = True
    conn.rollback()
check("UNIQUE cell_id blocks double booking", raised)
conn.close()

print("10) truly concurrent confirms (threads)")
# fresh pool
cc = [("K1", 100, 1.0), ("K2", 100, 1.0), ("K3", 100, 1.0),
      ("K4", 100, 1.0), ("K5", 101, 1.0), ("K6", 102, 1.0)]
bulk(cc, chem="K")
r = pre("K", cap=100, dc=5, dr=1)
ids = r.json()["candidate"]["candidate_ids"]
vers = r.json()["candidate"]["versions"]
results = []
barrier = threading.Barrier(4)

def worker():
    barrier.wait()
    body = confirm_body(ids, dict(vers), chem="K",
                        constraints={"min_capacity_ah": 100,
                                     "max_capacity_delta": 5,
                                     "max_resistance_delta": 1})
    rr = client.post("/confirm", json=body)
    results.append((rr.status_code,
                    rr.json().get("detail", {}).get("code") if rr.status_code != 200
                    else rr.json().get("group_no")))

ts = [threading.Thread(target=worker) for _ in range(4)]
[t.start() for t in ts]
[t.join() for t in ts]
winners = [x for x in results if x[0] == 200]
losers = [x for x in results if x[0] == 409 and x[1] == "OCCUPIED"]
check("exactly one concurrent winner", len(winners) == 1, results)
check("three OCCUPIED losers", len(losers) == 3, results)
# each cell occupied exactly once overall
conn = get_conn()
n_snap = conn.execute("SELECT COUNT(*) FROM group_snapshots WHERE cell_id IN ('K1','K2','K3')").fetchone()[0]
check("snapshots for contested cells written exactly once", n_snap == 3, n_snap)
conn.close()

print("11) constraint recheck at confirm (data changed since preselect)")
# preselect a combo, then revise a cell's capacity low enough to violate min
bulk([("M1", 200, 1.0), ("M2", 200, 1.0), ("M3", 201, 1.0), ("M4", 202, 1.0)], chem="M")
r = pre("M", cap=200, dc=5, dr=1)
ids = r.json()["candidate"]["candidate_ids"]
vers = dict(r.json()["candidate"]["versions"])
# raw-capacity tamper is not exposed via API (only test-version is revisable);
# simulate re-tested measurement directly in DB to prove defensive recheck
conn = get_conn()
conn.execute("UPDATE cells SET capacity_ah=150 WHERE id=?", (ids[0],))
conn.commit()
conn.close()
rr = client.post("/confirm", json=confirm_body(
    ids, vers, chem="M",
    constraints={"min_capacity_ah": 200, "max_capacity_delta": 5,
                 "max_resistance_delta": 1}))
check("CONSTRAINT_VIOLATED after measurement change",
      rr.status_code == 409 and rr.json()["detail"]["code"] == "CONSTRAINT_VIOLATED",
      rr.text)

print(f"\nALL {passed} CHECKS PASSED")
