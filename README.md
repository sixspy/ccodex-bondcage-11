# 保证金笼

> 遵守 `ops/底座生成前必读_效率与技术栈.md`（≥2000 行像产品）。  
> FastAPI + Vue 3 + SQLite；`docker compose up -d --build` 一键起。  
> **禁止同构换皮**：主链路不是「占位→确认入册」也不是「入轨→裁定赔付」也不是「业务窗裁切物化」。

## 骨架一句话

保证金先入笼冻结，履约核验通过才解笼拨付，违约进罚没册，笼代与拨付罚没对账。

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名 | 保证金笼 |
| 目录 | `11-bondcage` |
| 接口 | http://127.0.0.1:11321 |
| 库 | `data/app.db` |
| 账号 | admin / clerk / viewer ，密码均为 123456（viewer 只读） |

## 厚度

| 项 | 内容 |
|----|------|
| 业务 LOC | **≥2900**（py+前端，不含 vue.global / Docker / README；自检约 2984） |
| 后端模块 | db / auth / locks / bonds_svc / cages_svc / attest_svc / release_svc / forfeit_svc / reconcile_svc / audit_svc / topup_svc / split_svc / merge_svc |
| 前端视图 | 保证金户 / 入笼冻结（含同约多笼合并写口）/ 履约核验 / 解笼拨付 / 罚没册 / 对账互证 / 审计争用 / 增补拆笼空桩（8 页，导航会话保留） |
| 空桩 | topup（增补入笼）、split（拆笼分拨）——前后端均有 |
| 合并口径 | 甲路径·整笔同事务（锁死，乙路径桥接影子晋升禁用）；见 `docs/multi-cage-merge-minutes.md` |

## 主链路

开户入金 → 按合约入笼冻结（笼代）→ 同约多笼可整笔合并为一只主笼 → 履约核验通过 → 解笼拨付到收款方 / 违约罚没 → 笼代余额与拨付+罚没对账

## 多笼合并（甲路径，当日锁死）

- 同一事务：作废来源笼（`已合并作废`，行保留）→ 金额累加写入主笼（笼代 +1）→ 按活笼可用合计重算账户冻结 → 写 `cage_merge` 审计与 `merge_jobs/merge_lineage` 血缘；
- 任一步失败整笔回滚，作废与累加同滚，来源笼与账户零改（`merge_svc.FAIL_AFTER_ACCUMULATE` 故障注入由失败测守）；
- 乙路径（桥接影子主笼→晋升）禁用：`mode=影子晋升` 或影子态笼 → 记 `merge_yaw` 审计并 409 拒写；
- 同当事人合并抢账户锁 `bond:{party_key}`，后到只留 `contention_log` 争用痕、业务零写；
- 五页同源：入笼/账户/对账/核验/审计共用 `LIVE_CAGE_STATUSES` 与 `live_frozen_sum`，恒等式 `bond_accounts.frozen == SUM(活笼.available)`；
- 库路径不变（`data/app.db`），导航不增删，合并写口落在「入笼冻结」页；规则见 `docs/multi-cage-merge-minutes.md`。

## 互斥切口候选

- 解笼失败整笔回滚 vs 影子拨付后晋升
- 罚没立即入册 vs 影子罚没待复核晋升
- 增补同笼扩代 vs 新笼桥接双写
- 拆笼整笔事务切分 vs 影子子笼后晋升

## 启动

```powershell
cd projects/11-bondcage
docker compose up -d --build
```

浏览器：http://127.0.0.1:11321/

本机开发：

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10608
python -m pytest -q
```
