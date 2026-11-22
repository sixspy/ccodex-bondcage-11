"""对账互证：笼面可用 + 已拨付 + 已罚没 须等于笼面金额（同代口径）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import LIVE_CAGE_IN, LIVE_CAGE_STATUSES, MERGED_VOID_STATUS, connect

router = APIRouter(tags=["reconcile"])


def _reconcile_cage(conn, cage) -> dict:
    cage_id = cage["id"]
    gen = int(cage["generation"])
    amount = float(cage["amount"])
    available = float(cage["available"])

    # 已合并作废笼：金额已在同事务并入主笼，按血缘单列，不计失衡
    if cage["status"] == MERGED_VOID_STATUS:
        lineage = conn.execute(
            """SELECT l.*, j.main_cage_id, j.contract_no, m.cage_code AS main_cage_code
               FROM merge_lineage l
               JOIN merge_jobs j ON j.id=l.merge_job_id
               JOIN cages m ON m.id=j.main_cage_id
               WHERE l.source_cage_id=? ORDER BY l.id DESC LIMIT 1""",
            (cage_id,),
        ).fetchone()
        return {
            "cage_id": cage_id,
            "cage_code": cage["cage_code"],
            "party_key": cage["party_key"],
            "contract_no": cage["contract_no"],
            "generation": gen,
            "status": cage["status"],
            "amount": amount,
            "available": 0.0,
            "released": 0.0,
            "forfeited": 0.0,
            "accounted": amount,
            "balanced": True,
            "merged": True,
            "gap": 0.0,
            "merged_into": dict(lineage) if lineage else None,
            "proof": {
                "formula": "作废笼金额已整笔并入主笼（血缘见 merged_into）",
                "left": amount,
                "right": amount,
            },
        }

    released = conn.execute(
        """SELECT COALESCE(SUM(amount),0) s FROM releases
           WHERE cage_id=? AND status='已拨付' AND cage_generation=?""",
        (cage_id, gen),
    ).fetchone()["s"]
    forfeited = conn.execute(
        """SELECT COALESCE(SUM(amount),0) s FROM forfeits
           WHERE cage_id=? AND status='已罚没' AND cage_generation=?""",
        (cage_id, gen),
    ).fetchone()["s"]
    accounted = round(float(available) + float(released) + float(forfeited), 6)
    ok = abs(accounted - amount) < 1e-6
    # 账户侧：该当事人冻结额应覆盖所有仍冻结/核验中的笼可用（五页同源口径）
    party = cage["party_key"]
    acct = conn.execute(
        "SELECT balance, frozen FROM bond_accounts WHERE party_key=?",
        (party,),
    ).fetchone()
    live_frozen = conn.execute(
        f"""SELECT COALESCE(SUM(available),0) s FROM cages
           WHERE party_key=? AND status IN ({LIVE_CAGE_IN})""",
        (party, *LIVE_CAGE_STATUSES),
    ).fetchone()["s"]
    return {
        "cage_id": cage_id,
        "cage_code": cage["cage_code"],
        "party_key": party,
        "contract_no": cage["contract_no"],
        "generation": gen,
        "status": cage["status"],
        "amount": amount,
        "available": available,
        "released": float(released),
        "forfeited": float(forfeited),
        "accounted": accounted,
        "balanced": ok,
        "merged": False,
        "gap": round(amount - accounted, 6),
        "account_balance": float(acct["balance"]) if acct else None,
        "account_frozen": float(acct["frozen"]) if acct else None,
        "live_cage_available": float(live_frozen),
        "frozen_aligned": acct is not None
        and abs(float(acct["frozen"]) - float(live_frozen)) < 1e-6,
        "proof": {
            "formula": "available + released + forfeited == amount（同笼代）",
            "left": accounted,
            "right": amount,
        },
    }


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        cages = conn.execute("SELECT * FROM cages ORDER BY id").fetchall()
        items = [_reconcile_cage(conn, c) for c in cages]
        bad = [i for i in items if not i["balanced"]]
        merged_n = sum(1 for i in items if i.get("merged"))
        return {
            "items": items,
            "summary": {
                "total": len(items),
                "balanced": len(items) - len(bad),
                "unbalanced": len(bad),
                "merged_void": merged_n,
            },
        }


@router.get("/api/reconcile/{cage_code}")
def reconcile_one(cage_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        cage = conn.execute(
            "SELECT * FROM cages WHERE cage_code=?", (cage_code,)
        ).fetchone()
        if not cage:
            raise HTTPException(404, "笼不存在")
        return _reconcile_cage(conn, cage)


@router.get("/api/proof/{party_key}")
def party_proof(party_key: str, authorization: AuthHeader = None):
    """当事人级互证：账户冻结 vs 活笼可用合计。"""
    require_user(authorization)
    with connect() as conn:
        acct = conn.execute(
            "SELECT * FROM bond_accounts WHERE party_key=?", (party_key,)
        ).fetchone()
        if not acct:
            raise HTTPException(404, "保证金户不存在")
        live = conn.execute(
            f"""SELECT COALESCE(SUM(available),0) s FROM cages
               WHERE party_key=? AND status IN ({LIVE_CAGE_IN})""",
            (party_key, *LIVE_CAGE_STATUSES),
        ).fetchone()["s"]
        released = conn.execute(
            """SELECT COALESCE(SUM(r.amount),0) s FROM releases r
               JOIN cages c ON c.id=r.cage_id
               WHERE c.party_key=? AND r.status='已拨付'""",
            (party_key,),
        ).fetchone()["s"]
        forfeited = conn.execute(
            """SELECT COALESCE(SUM(f.amount),0) s FROM forfeits f
               JOIN cages c ON c.id=f.cage_id
               WHERE c.party_key=? AND f.status='已罚没'""",
            (party_key,),
        ).fetchone()["s"]
        frozen = float(acct["frozen"])
        live_f = float(live)
        return {
            "party_key": party_key,
            "account": dict(acct),
            "live_cage_available": live_f,
            "released_total": float(released),
            "forfeited_total": float(forfeited),
            "frozen_matches_live": abs(frozen - live_f) < 1e-6,
            "proof": {
                "formula": "account.frozen == sum(live cage available)",
                "left": frozen,
                "right": live_f,
            },
        }
