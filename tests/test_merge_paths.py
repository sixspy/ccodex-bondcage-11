"""多笼合并正向与偏航用例（甲路径整笔同事务）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

CONTRACT = "HT-2026-1101"
PARTY = "P-EAST"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _group(c: TestClient, h: dict) -> list[dict]:
    items = c.get("/api/cages", headers=h).json()["items"]
    rows = [
        x
        for x in items
        if x["party_key"] == PARTY
        and x["contract_no"] == CONTRACT
        and x["status"] == "已冻结"
    ]
    return sorted(rows, key=lambda x: x["id"])


def _do_merge(c: TestClient, h: dict, mode: str = "整笔同事务"):
    rows = _group(c, h)
    main, sources = rows[0], rows[1:]
    return rows, c.post(
        "/api/cages/merge",
        headers=h,
        json={
            "party_key": PARTY,
            "contract_no": CONTRACT,
            "main_cage_id": main["id"],
            "source_cage_ids": [x["id"] for x in sources],
            "mode": mode,
        },
    )


def test_merge_happy_path_leaves_single_main_cage():
    c = TestClient(app)
    h = _auth(c)
    rows, r = _do_merge(c, h)
    assert r.status_code == 200, r.text
    body = r.json()
    total = sum(float(x["amount"]) for x in rows)

    # 只剩主笼为活笼，来源笼全部作废
    with connect() as conn:
        live = conn.execute(
            """SELECT * FROM cages WHERE party_key=? AND contract_no=?
               AND status='已冻结'""",
            (PARTY, CONTRACT),
        ).fetchall()
        assert len(live) == 1
        main_row = live[0]
        assert main_row["id"] == rows[0]["id"]
        assert abs(float(main_row["amount"]) - total) < 1e-6
        assert abs(float(main_row["available"]) - total) < 1e-6
        assert main_row["generation"] == 2
        voided = conn.execute(
            """SELECT COUNT(*) c FROM cages WHERE status='已合并作废'
               AND id IN (%s)""" % ",".join("?" * (len(rows) - 1)),
            [x["id"] for x in rows[1:]],
        ).fetchone()["c"]
        assert voided == len(rows) - 1
        acct = conn.execute(
            "SELECT balance,frozen FROM bond_accounts WHERE party_key=?", (PARTY,)
        ).fetchone()
        assert abs(float(acct["frozen"]) - total) < 1e-6
        assert float(acct["balance"]) == 500000.0

    # 对账与当事人互证同源对齐
    recon = c.get(f"/api/reconcile/{body['main_cage_code']}", headers=h).json()
    assert recon["balanced"] is True
    assert recon["frozen_aligned"] is True
    proof = c.get(f"/api/proof/{PARTY}", headers=h).json()
    assert proof["frozen_matches_live"] is True
    all_recon = c.get("/api/reconcile", headers=h).json()
    assert all_recon["summary"]["unbalanced"] == 0
    assert all_recon["summary"]["merged_void"] == len(rows) - 1

    # 血缘与审计
    jobs = c.get("/api/merge/jobs", headers=h).json()["items"]
    assert len(jobs) == 1
    assert len(jobs[0]["lineage"]) == len(rows) - 1
    with connect() as conn:
        audit = conn.execute(
            "SELECT action FROM audit_trail WHERE action='cage_merge'"
        ).fetchall()
        assert len(audit) == 1


def test_yaw_shadow_mode_rejected_and_audited():
    c = TestClient(app)
    h = _auth(c)
    _, r = _do_merge(c, h, mode="影子晋升")
    assert r.status_code == 409
    with connect() as conn:
        yaw = conn.execute(
            "SELECT action,target,payload FROM audit_trail WHERE action='merge_yaw'"
        ).fetchall()
        assert len(yaw) == 1
        assert "影子晋升" in yaw[0]["payload"]
        # 业务零写
        assert conn.execute("SELECT COUNT(*) c FROM merge_jobs").fetchone()["c"] == 0
        live = conn.execute(
            """SELECT COUNT(*) c FROM cages WHERE party_key=? AND contract_no=?
               AND status='已冻结'""",
            (PARTY, CONTRACT),
        ).fetchone()["c"]
        assert live == 4


def test_reject_cross_contract_and_double_merge():
    c = TestClient(app)
    h = _auth(c)
    rows, ok = _do_merge(c, h)
    assert ok.status_code == 200
    # 再次合并同组：来源笼已作废，须拒写且不新增作业
    main, sources = rows[0], rows[1:]
    again = c.post(
        "/api/cages/merge",
        headers=h,
        json={
            "party_key": PARTY,
            "contract_no": CONTRACT,
            "main_cage_id": main["id"],
            "source_cage_ids": [x["id"] for x in sources],
            "mode": "整笔同事务",
        },
    )
    assert again.status_code == 409
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM merge_jobs").fetchone()["c"] == 1

    # 跨合约合并拒写（主笼 P-EAST 合约 vs 北港笼）
    other = [
        x
        for x in c.get("/api/cages", headers=h).json()["items"]
        if x["cage_code"] == "CG-P-NORTH-001"
    ][0]
    cross = c.post(
        "/api/cages/merge",
        headers=h,
        json={
            "party_key": PARTY,
            "contract_no": CONTRACT,
            "main_cage_id": main["id"],
            "source_cage_ids": [other["id"]],
            "mode": "整笔同事务",
        },
    )
    assert cross.status_code == 409


def test_merged_void_cage_cannot_attest():
    c = TestClient(app)
    h = _auth(c)
    rows, ok = _do_merge(c, h)
    assert ok.status_code == 200
    voided_id = rows[1]["id"]
    r = c.post(
        "/api/attestations",
        headers=h,
        json={"cage_id": voided_id, "outcome": "通过", "evidence": "x"},
    )
    assert r.status_code == 409
    # 主笼仍可正常核验→解笼，账实不断
    main_id = rows[0]["id"]
    assert (
        c.post(
            "/api/attestations",
            headers=h,
            json={"cage_id": main_id, "outcome": "通过", "evidence": "竣工"},
        ).status_code
        == 200
    )
    rel = c.post("/api/releases", headers=h, json={"cage_id": main_id})
    assert rel.status_code == 200
    proof = c.get(f"/api/proof/{PARTY}", headers=h).json()
    assert proof["frozen_matches_live"] is True


def test_viewer_cannot_merge():
    c = TestClient(app)
    admin_h = _auth(c)
    h = _auth(c, "viewer")
    rows = _group(c, admin_h)
    r = c.post(
        "/api/cages/merge",
        headers=h,
        json={
            "party_key": PARTY,
            "contract_no": CONTRACT,
            "main_cage_id": rows[0]["id"],
            "source_cage_ids": [rows[1]["id"]],
            "mode": "整笔同事务",
        },
    )
    assert r.status_code == 403
