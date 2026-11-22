"""多笼合并写口 —— 甲路径：合并整笔同事务。

口径锁死见 docs/multi-cage-merge-minutes.md：
- 同一事务内作废来源笼、累加金额写入主笼、重算账户冻结、记审计与血缘；
- 任一步失败整笔回滚（作废与累加同滚），来源笼与账户零改；
- 乙路径（桥接影子主笼→晋升）禁用，夹带即按合并偏航 merge_yaw 拒写；
- 同当事人合并抢账户锁 bond:{party_key}，后到只留 contention_log 争用痕。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import (
    LIVE_CAGE_IN,
    LIVE_CAGE_STATUSES,
    MERGED_VOID_STATUS,
    SHADOW_STATUS,
    connect,
    now_iso,
    write_audit,
)
from locks import release, try_acquire
from models import MergeIn

router = APIRouter(tags=["merge"])

# 测试故障注入点：主笼累加之后、提交之前炸点，验证整笔回滚。
FAIL_AFTER_ACCUMULATE = False

MERGE_MODE = "整笔同事务"
YAW_MODE = "影子晋升"


def live_frozen_sum(conn, party_key: str) -> float:
    """账户冻结同源口径：活笼可用合计（五页共用）。"""
    row = conn.execute(
        f"""SELECT COALESCE(SUM(available),0) s FROM cages
            WHERE party_key=? AND status IN ({LIVE_CAGE_IN})""",
        (party_key, *LIVE_CAGE_STATUSES),
    ).fetchone()
    return round(float(row["s"]), 6)


def _record_yaw(user: str, reason: str, payload: dict) -> None:
    """偏航独立短事务落审计，不受业务事务回滚影响。"""
    with connect() as conn:
        write_audit(
            conn,
            user,
            "merge_yaw",
            payload.get("contract_no", "?"),
            {"reason": reason, **payload},
        )
        conn.commit()


def _load_cage(conn, cage_id: int) -> dict:
    row = conn.execute("SELECT * FROM cages WHERE id=?", (cage_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"笼 {cage_id} 不存在")
    return dict(row)


@router.get("/api/merge/jobs")
def list_merge_jobs(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        jobs = conn.execute(
            "SELECT * FROM merge_jobs ORDER BY id DESC LIMIT 200"
        ).fetchall()
        items = []
        for j in jobs:
            lines = conn.execute(
                "SELECT * FROM merge_lineage WHERE merge_job_id=? ORDER BY id",
                (j["id"],),
            ).fetchall()
            items.append({"job": dict(j), "lineage": [dict(x) for x in lines]})
        return {"items": items}


@router.post("/api/cages/merge")
def merge_cages(body: MergeIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"bond:{body.party_key}"

    # —— 偏航闸门：乙路径禁用，两边不得混用 ——
    if body.mode != MERGE_MODE:
        _record_yaw(
            user,
            f"禁用口径：仅允许 {MERGE_MODE}，收到 {body.mode}",
            {
                "party_key": body.party_key,
                "contract_no": body.contract_no,
                "mode": body.mode,
                "main_cage_id": body.main_cage_id,
                "source_cage_ids": list(body.source_cage_ids),
            },
        )
        raise HTTPException(409, f"合并偏航：{YAW_MODE} 等乙路径已禁用，拒写")

    source_ids = list(dict.fromkeys(int(x) for x in body.source_cage_ids))
    if not source_ids:
        raise HTTPException(400, "至少需要一只来源笼")
    if body.main_cage_id in source_ids:
        raise HTTPException(400, "主笼不得同时作为来源笼")

    # —— 抢账户锁：独立短连接提交，争用痕不随业务回滚 ——
    with connect() as lock_conn:
        acquired = try_acquire(lock_conn, lock_key, user)
        lock_conn.commit()
    if not acquired:
        raise HTTPException(409, "争用：后到只记争用，不写业务")

    try:
        # connect() 上下文：正常退出提交，异常退出整笔回滚（作废与累加同滚）
        with connect() as conn:
            try:
                main = _load_cage(conn, body.main_cage_id)
                sources = [_load_cage(conn, cid) for cid in source_ids]
                all_cages = [main, *sources]

                # —— 准入校验：同当事人、同合约、同状态、无核验、无影子态 ——
                for cg in all_cages:
                    if cg["party_key"] != body.party_key:
                        raise HTTPException(409, "合并笼必须同当事人")
                    if cg["contract_no"] != body.contract_no:
                        raise HTTPException(409, "合并笼必须同合约号")
                    if cg["status"] == SHADOW_STATUS:
                        _record_yaw(
                            user,
                            "影子态笼禁入甲路径合并",
                            {
                                "party_key": body.party_key,
                                "contract_no": body.contract_no,
                                "cage_code": cg["cage_code"],
                            },
                        )
                        raise HTTPException(409, "合并偏航：影子态笼拒写")
                    if cg["status"] == MERGED_VOID_STATUS:
                        raise HTTPException(409, f"{cg['cage_code']} 已作废，禁止再并")
                    if cg["status"] != "已冻结":
                        raise HTTPException(409, f"{cg['cage_code']} 非已冻结笼，禁止合并")
                    att_n = conn.execute(
                        "SELECT COUNT(*) c FROM attestations WHERE cage_id=?",
                        (cg["id"],),
                    ).fetchone()["c"]
                    if att_n:
                        raise HTTPException(409, f"{cg['cage_code']} 已有核验结论，禁止合并")

                added = round(sum(float(c["available"]) for c in sources), 6)
                if added <= 0:
                    raise HTTPException(400, "来源笼金额合计须大于 0")
                expect_amount = round(float(main["amount"]) + added, 6)
                expect_available = round(float(main["available"]) + added, 6)

                acct = conn.execute(
                    "SELECT * FROM bond_accounts WHERE party_key=?",
                    (body.party_key,),
                ).fetchone()
                if not acct:
                    raise HTTPException(404, "保证金户不存在")
                frozen_before = round(float(acct["frozen"]), 6)
                balance_before = round(float(acct["balance"]), 6)
                now = now_iso()

                # 1) 合并作业与血缘
                job = conn.execute(
                    """INSERT INTO merge_jobs(party_key,contract_no,main_cage_id,mode,
                       merged_amount,status,actor,detail,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        body.party_key,
                        body.contract_no,
                        main["id"],
                        MERGE_MODE,
                        expect_amount,
                        "已合并",
                        user,
                        f"{len(sources)} 只来源笼并入 {main['cage_code']}",
                        now,
                    ),
                )
                job_id = job.lastrowid
                for cg in sources:
                    conn.execute(
                        """INSERT INTO merge_lineage(merge_job_id,source_cage_id,
                           source_cage_code,amount,created_at)
                           VALUES (?,?,?,?,?)""",
                        (job_id, cg["id"], cg["cage_code"], float(cg["available"]), now),
                    )

                # 2) 同一提交作废来源笼（账实不断：金额先在主笼侧承接）
                placeholders = ",".join("?" for _ in source_ids)
                conn.execute(
                    f"""UPDATE cages SET status=?, available=0.0
                        WHERE id IN ({placeholders})""",
                    (MERGED_VOID_STATUS, *source_ids),
                )

                # 3) 累加金额写入主笼，笼代加 1
                new_generation = int(main["generation"]) + 1
                conn.execute(
                    """UPDATE cages SET amount=?, available=?, generation=?
                       WHERE id=?""",
                    (expect_amount, expect_available, new_generation, main["id"]),
                )

                # 故障注入：模拟合并未收口，须触发整笔回滚
                if FAIL_AFTER_ACCUMULATE:
                    raise RuntimeError("注入故障：合并未收口（FAIL_AFTER_ACCUMULATE）")

                # 4) 重算账户冻结（同源口径：活笼可用合计），余额不动
                live_sum = live_frozen_sum(conn, body.party_key)
                if abs(live_sum - frozen_before) > 1e-6:
                    raise HTTPException(
                        500,
                        f"账户冻结重算不平：活笼合计 {live_sum} != 原冻结 {frozen_before}",
                    )
                conn.execute(
                    "UPDATE bond_accounts SET frozen=? WHERE party_key=?",
                    (live_sum, body.party_key),
                )
                acct_after = conn.execute(
                    "SELECT balance,frozen FROM bond_accounts WHERE party_key=?",
                    (body.party_key,),
                ).fetchone()
                if abs(float(acct_after["balance"]) - balance_before) > 1e-9:
                    raise HTTPException(500, "合并不得改动账户余额")

                # 5) 审计
                write_audit(
                    conn,
                    user,
                    "cage_merge",
                    main["cage_code"],
                    {
                        "merge_job_id": job_id,
                        "mode": MERGE_MODE,
                        "party_key": body.party_key,
                        "contract_no": body.contract_no,
                        "main_cage_id": main["id"],
                        "main_cage_code": main["cage_code"],
                        "source_cage_ids": source_ids,
                        "source_cage_codes": [c["cage_code"] for c in sources],
                        "added_amount": added,
                        "merged_amount": expect_amount,
                        "generation_from": int(main["generation"]),
                        "generation_to": new_generation,
                        "account_frozen": live_sum,
                    },
                )
                conn.commit()

                row = conn.execute(
                    "SELECT * FROM cages WHERE id=?", (main["id"],)
                ).fetchone()
                return {
                    "merge_job_id": job_id,
                    "main_cage_id": main["id"],
                    "main_cage_code": main["cage_code"],
                    "generation": new_generation,
                    "amount": expect_amount,
                    "available": expect_available,
                    "voided_sources": source_ids,
                    "account_frozen": live_sum,
                    "cage": dict(row),
                }
            except HTTPException:
                # 业务拒写：连接上下文负责回滚，异常透传为对应 4xx/5xx
                raise
            except Exception as exc:  # noqa: BLE001 - 故障注入与未预期错误一律整笔回滚
                raise HTTPException(500, f"合并未收口，已整笔回滚：{exc}") from exc
    finally:
        # 锁释放走独立连接，业务回滚不影响解锁
        with connect() as rel_conn:
            release(rel_conn, lock_key, user)
            rel_conn.commit()
