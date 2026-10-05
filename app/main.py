"""FastAPI application: cell intake, pre-selection and atomic confirmation."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException

from . import grouping
from .config import EPS, MAX_CANDIDATES
from .db import get_conn, init_db, transaction
from .schemas import (
    CellOut,
    CellsBulkIn,
    ConfirmIn,
    ConfirmResponse,
    GroupOut,
    GroupSnapshotCell,
    PreselectConstraints,
    PreselectIn,
    PreselectResponse,
    RevisionIn,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="退役电芯成组放行 API",
    description=(
        "录入最多 12 节待选退役电芯；按串联节数、最低单芯容量及容量差/内阻差 "
        "预选同体系未占用电芯组；确认时在单个 SQLite 事务中重查检测版本与占用状态，"
        "原子占用整组并保存不可改写的检测快照。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def error(code: str, message: str, http_status: int = 400, **extra) -> HTTPException:
    detail = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=http_status, detail=detail)


# ---------------------------------------------------------------- intake

@app.post("/cells", status_code=201, response_model=List[CellOut],
          summary="批量录入待选电芯（每次最多 12 节）")
def create_cells(payload: CellsBulkIn,
                 conn: sqlite3.Connection = Depends(get_conn)):
    try:
        with transaction(conn):
            for c in payload.cells:
                try:
                    conn.execute(
                        "INSERT INTO cells (id, chemistry, capacity_ah, "
                        "resistance_mohm, test_version) VALUES (?,?,?,?,?)",
                        (c.id, c.chemistry, c.capacity_ah,
                         c.resistance_mohm, c.test_version),
                    )
                except sqlite3.IntegrityError:
                    raise error(
                        "DUPLICATE_CELL_ID",
                        f"电芯 ID 已存在: {c.id}", 409)
        rows = conn.execute(
            "SELECT * FROM cells WHERE id IN (%s)"
            % ",".join("?" * len(payload.cells)),
            [c.id for c in payload.cells],
        ).fetchall()
        return [_cell_out(r) for r in rows]
    finally:
        conn.close()


@app.get("/cells", response_model=List[CellOut], summary="查询电芯（可按体系/占用状态过滤）")
def list_cells(chemistry: Optional[str] = None, occupied: Optional[bool] = None,
               conn: sqlite3.Connection = Depends(get_conn)):
    sql = "SELECT * FROM cells WHERE 1=1"
    params: list = []
    if chemistry is not None:
        sql += " AND chemistry = ?"
        params.append(chemistry)
    if occupied is True:
        sql += " AND occupied_group IS NOT NULL"
    elif occupied is False:
        sql += " AND occupied_group IS NULL"
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_cell_out(r) for r in rows]


@app.patch("/cells/{cell_id}/test-version", response_model=CellOut,
           summary="修订电芯检测版本（已确认组的快照不受影响）")
def revise_version(cell_id: str, payload: RevisionIn,
                   conn: sqlite3.Connection = Depends(get_conn)):
    try:
        cur = conn.execute(
            "UPDATE cells SET test_version = ? WHERE id = ?",
            (payload.test_version, cell_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise error("CELL_NOT_FOUND", f"电芯不存在: {cell_id}", 404)
        row = conn.execute("SELECT * FROM cells WHERE id = ?", (cell_id,)).fetchone()
        return _cell_out(row)
    finally:
        conn.close()


# ------------------------------------------------------------ pre-select

@app.post("/preselect", response_model=PreselectResponse,
          summary="预选一组同体系、未占用的电芯")
def preselect(payload: PreselectIn,
              conn: sqlite3.Connection = Depends(get_conn)):
    try:
        rows = conn.execute(
            "SELECT id, chemistry, capacity_ah, resistance_mohm, test_version "
            "FROM cells WHERE chemistry = ? AND occupied_group IS NULL ORDER BY id",
            (payload.chemistry,),
        ).fetchall()
        cells = [grouping.Cell(r["id"], r["chemistry"], r["capacity_ah"],
                               r["resistance_mohm"], r["test_version"])
                 for r in rows]
    finally:
        conn.close()

    constraints = PreselectConstraints(
        chemistry=payload.chemistry,
        min_size=payload.min_size,
        max_size=payload.max_size,
        min_capacity_ah=payload.min_capacity_ah,
        max_capacity_delta=payload.max_capacity_delta,
        max_resistance_delta=payload.max_resistance_delta,
    )

    if len(cells) > MAX_CANDIDATES:
        # Guard rail: the pending pool is capped at 12 cells per chemistry.
        raise error(
            "POOL_TOO_LARGE",
            f"体系 {payload.chemistry} 下有 {len(cells)} 节未占用电芯，"
            f"超过单次待选上限 {MAX_CANDIDATES}；请先确认成组或缩小范围",
        )

    found = grouping.find_best(
        cells, payload.min_size, payload.max_size,
        payload.min_capacity_ah, payload.max_capacity_delta,
        payload.max_resistance_delta,
    )
    if found is None:
        code, reason = grouping.diagnose(
            cells, payload.min_size, payload.max_size,
            payload.min_capacity_ah, payload.max_capacity_delta,
            payload.max_resistance_delta,
        )
        return PreselectResponse(feasible=False, reason_code=code,
                                 reason=reason, pool_size=len(cells))

    combo, limiting, cap_spread, res_spread = found
    ids = sorted(c.id for c in combo)
    versions = {c.id: c.version for c in combo}
    candidate = {
        "candidate_ids": ids,
        "size": len(ids),
        "limiting_capacity_ah": round(limiting, 6),
        "capacity_spread_ah": round(cap_spread, 6),
        "resistance_spread_mohm": round(res_spread, 6),
        "versions": versions,
        "constraints": constraints,
    }
    return PreselectResponse(feasible=True, candidate=candidate,
                             pool_size=len(cells))


# -------------------------------------------------------------- confirm

@app.post("/confirm", response_model=ConfirmResponse,
          summary="确认候选：单事务重查版本/占用并原子占用整组")
def confirm(payload: ConfirmIn,
            conn: sqlite3.Connection = Depends(get_conn)):
    ids = payload.candidate_ids
    try:
        # Acquire the write lock up front so two racing confirmations serialize.
        with transaction(conn, immediate=True):
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT id, chemistry, capacity_ah, resistance_mohm, "
                f"test_version, occupied_group FROM cells "
                f"WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
            by_id = {r["id"]: r for r in rows}

            missing = [cid for cid in ids if cid not in by_id]
            if missing:
                raise error(
                    "MISSING_CELLS",
                    f"候选中存在未录入电芯: {missing}", 404, missing=missing)

            # Full re-check inside the transaction; collect every staleness
            # reason so the client gets a complete explanation.
            occupied = [cid for cid in ids
                        if by_id[cid]["occupied_group"] is not None]
            version_conflicts = [
                cid for cid in ids
                if by_id[cid]["test_version"] != payload.versions[cid]
            ]
            wrong_chem = [cid for cid in ids
                          if by_id[cid]["chemistry"] != payload.chemistry]

            rechecked = [
                grouping.Cell(r["id"], r["chemistry"], r["capacity_ah"],
                              r["resistance_mohm"], r["test_version"])
                for r in rows
            ]
            constraint_violations = _recheck_constraints(rechecked, payload)

            if occupied or version_conflicts or wrong_chem or constraint_violations:
                details = {}
                if occupied:
                    details["occupied"] = occupied
                if version_conflicts:
                    details["version_conflicts"] = [
                        {"cell_id": cid,
                         "expected": payload.versions[cid],
                         "current": by_id[cid]["test_version"]}
                        for cid in version_conflicts
                    ]
                if wrong_chem:
                    details["wrong_chemistry"] = wrong_chem
                if constraint_violations:
                    details["constraint_violations"] = constraint_violations
                code = ("OCCUPIED" if occupied
                        else "VERSION_CONFLICT" if version_conflicts
                        else "CONSTRAINT_VIOLATED")
                raise error(
                    code, "候选已过期或不再满足放行条件，整组拒绝（未产生任何占用）",
                    409, **details)

            limiting = min(r["capacity_ah"] for r in rows)
            cur = conn.execute(
                "INSERT INTO groups (chemistry, min_size, max_size, "
                "min_capacity_ah, max_capacity_delta, max_res_delta, "
                "limiting_capacity) VALUES (?,?,?,?,?,?,?)",
                (payload.chemistry, payload.min_size, payload.max_size,
                 payload.min_capacity_ah, payload.max_capacity_delta,
                 payload.max_resistance_delta, limiting),
            )
            group_no = cur.lastrowid

            ordered = sorted(rows, key=lambda r: r["id"])
            for pos, r in enumerate(ordered, start=1):
                conn.execute(
                    "INSERT INTO group_snapshots (group_id, cell_id, position, "
                    "chemistry, capacity_ah, resistance_mohm, test_version) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (group_no, r["id"], pos, r["chemistry"], r["capacity_ah"],
                     r["resistance_mohm"], r["test_version"]),
                )
                conn.execute(
                    "UPDATE cells SET occupied_group = ? WHERE id = ?",
                    (group_no, r["id"]),
                )
            # Leaving the context commits group + snapshots + occupancy atomically;
            # any failure rolls back, so a partial occupancy is impossible.

        out_rows = conn.execute(
            "SELECT cell_id, position, chemistry, capacity_ah, "
            "resistance_mohm, test_version FROM group_snapshots "
            "WHERE group_id = ? ORDER BY position", (group_no,),
        ).fetchall()
        return ConfirmResponse(
            group_no=group_no,
            candidate_ids=[r["cell_id"] for r in out_rows],
            limiting_capacity_ah=limiting,
            versions={r["cell_id"]: r["test_version"] for r in out_rows},
            snapshot_cells=[dict(r) for r in out_rows],
        )
    finally:
        conn.close()


def _recheck_constraints(cells: List[grouping.Cell],
                         payload: ConfirmIn) -> List[dict]:
    """Re-validate the engineering constraints at confirmation time."""
    violations: List[dict] = []
    n = len(cells)
    if not (payload.min_size <= n <= payload.max_size):
        violations.append({
            "type": "SIZE",
            "message": f"候选节数 {n} 不在 {payload.min_size}~{payload.max_size} 范围内",
        })
    for c in cells:
        if c.capacity + EPS < payload.min_capacity_ah:
            violations.append({
                "type": "MIN_CAPACITY", "cell_id": c.id,
                "capacity_ah": c.capacity,
                "message": f"{c.id} 容量 {c.capacity:g} Ah 低于最低单芯容量",
            })
    if cells:
        cap_spread = max(c.capacity for c in cells) - min(c.capacity for c in cells)
        res_spread = (max(c.resistance for c in cells)
                      - min(c.resistance for c in cells))
        if cap_spread > payload.max_capacity_delta + EPS:
            violations.append({
                "type": "CAPACITY_SPREAD", "spread_ah": cap_spread,
                "message": f"容量差 {cap_spread:g} Ah 超过上限",
            })
        if res_spread > payload.max_resistance_delta + EPS:
            violations.append({
                "type": "RESISTANCE_SPREAD", "spread_mohm": res_spread,
                "message": f"内阻差 {res_spread:g} mΩ 超过上限",
            })
    return violations


# ------------------------------------------------------------ query groups

@app.get("/groups", response_model=List[GroupOut], summary="列出所有已确认组")
def list_groups(conn: sqlite3.Connection = Depends(get_conn)):
    try:
        groups = conn.execute("SELECT * FROM groups ORDER BY id").fetchall()
        return [_group_out(conn, g) for g in groups]
    finally:
        conn.close()


@app.get("/groups/{group_no}", response_model=GroupOut,
         summary="按组编号查询成组放行记录与不可改写快照")
def get_group(group_no: int, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        g = conn.execute("SELECT * FROM groups WHERE id = ?", (group_no,)).fetchone()
        if g is None:
            raise error("GROUP_NOT_FOUND", f"组编号不存在: {group_no}", 404)
        return _group_out(conn, g)
    finally:
        conn.close()


def _group_out(conn: sqlite3.Connection, g: sqlite3.Row) -> GroupOut:
    snap_rows = conn.execute(
        "SELECT * FROM group_snapshots WHERE group_id = ? ORDER BY position",
        (g["id"],),
    ).fetchall()
    return GroupOut(
        group_no=g["id"],
        chemistry=g["chemistry"],
        limiting_capacity_ah=g["limiting_capacity"],
        created_at=g["created_at"],
        cells=[GroupSnapshotCell(
            position=r["position"], cell_id=r["cell_id"],
            chemistry=r["chemistry"], capacity_ah=r["capacity_ah"],
            resistance_mohm=r["resistance_mohm"], test_version=r["test_version"],
        ) for r in snap_rows],
    )


def _cell_out(r: sqlite3.Row) -> CellOut:
    return CellOut(
        id=r["id"], chemistry=r["chemistry"], capacity_ah=r["capacity_ah"],
        resistance_mohm=r["resistance_mohm"], test_version=r["test_version"],
        occupied=r["occupied_group"] is not None,
        occupied_group=r["occupied_group"],
    )


@app.get("/health", summary="健康检查")
def health():
    return {"status": "ok"}
