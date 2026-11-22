"""多笼合并 TDD：先红后绿。

顺序锁死（见 docs/multi-cage-merge-minutes.md）：
1. 同约多种子笼假绿夹具基线（绿）；
2. 合并未收口失败测（先红，实现后转绿：失败须整笔回滚）；
3. 双人同合并争用失败测（先红，实现后转绿：后到只留争用痕）。
"""
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


def _same_contract_cages(c: TestClient, h: dict) -> list[dict]:
    items = c.get("/api/cages", headers=h).json()["items"]
    rows = [
        x
        for x in items
        if x["party_key"] == PARTY
        and x["contract_no"] == CONTRACT
        and x["status"] == "已冻结"
    ]
    return sorted(rows, key=lambda x: x["id"])


def test_fixture_false_green_same_contract_multi_cage():
    """假绿事故夹具：同约多笼各自对账全绿，却无合并写口可收口。"""
    c = TestClient(app)
    h = _auth(c)
    rows = _same_contract_cages(c, h)
    assert len(rows) >= 4, "夹具须先造出同约多只冻结笼"
    amounts = sorted(float(x["amount"]) for x in rows)
    assert amounts == [60000.0, 60000.0, 60000.0, 120000.0]
    # 假绿：逐笼对账都平衡
    for x in rows:
        recon = c.get(f"/api/reconcile/{x['cage_code']}", headers=h).json()
        assert recon["balanced"] is True
    proof = c.get(f"/api/proof/{PARTY}", headers=h).json()
    assert proof["frozen_matches_live"] is True
    # 事故面：没有任何合并写口能把它们收成一只
    assert (
        any(getattr(r, "path", None) == "/api/cages/merge" for r in app.routes)
        is False
    )


def test_merge_failure_rolls_back_whole_thing(monkeypatch):
    """失败测一：合并未收口时，作废与累加必须同滚，账实零改。"""
    import merge_svc  # 实现前导入即红

    c = TestClient(app)
    h = _auth(c)
    rows = _same_contract_cages(c, h)
    main, sources = rows[0], rows[1:]
    snapshot = {
        x["id"]: (x["status"], float(x["amount"]), float(x["available"]))
        for x in rows
    }
    with connect() as conn:
        acct_before = conn.execute(
            "SELECT balance,frozen FROM bond_accounts WHERE party_key=?", (PARTY,)
        ).fetchone()
        frozen_before = float(acct_before["frozen"])
        balance_before = float(acct_before["balance"])

    # 故障注入：主笼累加之后、事务提交之前炸点，模拟「合并未收口」
    monkeypatch.setattr(merge_svc, "FAIL_AFTER_ACCUMULATE", True, raising=False)

    r = c.post(
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
    assert r.status_code == 500
    monkeypatch.setattr(merge_svc, "FAIL_AFTER_ACCUMULATE", False, raising=False)

    # 来源笼与主笼零改
    after = {x["id"]: x for x in c.get("/api/cages", headers=h).json()["items"]}
    for cid, (status, amount, available) in snapshot.items():
        row = after[cid]
        assert row["status"] == status
        assert float(row["amount"]) == amount
        assert float(row["available"]) == available
    # 账户冻结/余额零改
    with connect() as conn:
        acct = conn.execute(
            "SELECT balance,frozen FROM bond_accounts WHERE party_key=?", (PARTY,)
        ).fetchone()
        assert float(acct["frozen"]) == frozen_before
        assert float(acct["balance"]) == balance_before
        assert conn.execute("SELECT COUNT(*) c FROM merge_jobs").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM merge_lineage").fetchone()["c"] == 0
        audits = conn.execute(
            "SELECT action FROM audit_trail WHERE action='cage_merge'"
        ).fetchall()
        assert audits == []


def test_concurrent_merge_contention_leaves_only_trace():
    """失败测二：两人同合并同一组笼，后到只留争用痕，业务零写。"""
    c = TestClient(app)
    h1 = _auth(c, "admin")
    h2 = _auth(c, "clerk")
    rows = _same_contract_cages(c, h1)
    main, sources = rows[0], rows[1:]
    payload = {
        "party_key": PARTY,
        "contract_no": CONTRACT,
        "main_cage_id": main["id"],
        "source_cage_ids": [x["id"] for x in sources],
        "mode": "整笔同事务",
    }

    # 第一人先占账户锁（模拟合并进行中）
    got = c.post(
        "/api/locks/acquire",
        headers=h1,
        json={"lock_key": f"bond:{PARTY}"},
    )
    assert got.status_code == 200

    r = c.post("/api/cages/merge", headers=h2, json=payload)
    assert r.status_code == 409

    # 后到只留争用痕
    contention = c.get("/api/contention", headers=h1).json()["items"]
    hits = [
        x
        for x in contention
        if x["lock_key"] == f"bond:{PARTY}" and x["actor"] == "clerk"
    ]
    assert hits, "后到合并必须登记争用痕"

    # 业务零写：无合并血缘、笼状态不变、无 cage_merge 审计
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM merge_lineage").fetchone()["c"] == 0
        live = conn.execute(
            """SELECT COUNT(*) c FROM cages
               WHERE party_key=? AND contract_no=? AND status='已冻结'""",
            (PARTY, CONTRACT),
        ).fetchone()["c"]
        assert live == len(rows)
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM audit_trail WHERE action='cage_merge'"
            ).fetchone()["c"]
            == 0
        )

    # 第一人完成后再合并应能成功（锁释放后无残留阻塞）
    assert (
        c.post(
            "/api/locks/release",
            headers=h1,
            json={"lock_key": f"bond:{PARTY}"},
        ).status_code
        == 200
    )
    ok = c.post("/api/cages/merge", headers=h1, json=payload)
    assert ok.status_code == 200
    assert ok.json()["main_cage_id"] == main["id"]
