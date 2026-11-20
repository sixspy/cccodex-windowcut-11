# 业务窗关账

> 遵守 `ops/底座生成前必读_效率与技术栈.md`（**≥3× 厚度**）。  
> FastAPI + Vue 3 + SQLite；交付须 `docker compose up -d --build` 一键起。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立。

## 骨架一句话

事件按业务窗裁切，迟到进旁路或拒收，关窗后物化结果并与窗口对账。

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 业务窗关账 |
| 目录 | `08-windowcut` |
| 接口 | http://127.0.0.1:11021 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 厚度（相对薄壳 ≥3×）

| 项 | 目标 |
|----|------|
| 业务 LOC | ≥900（目标 1000+） |
| 后端具名模块 | `db` / `auth` / `locks` / `windows_svc` / `ingest_svc` / `late_svc` / `materialize_svc` / `reconcile_svc` / `audit_svc` / `hold_svc` / `reopen_svc` |
| 前端视图 | 业务窗 / 入账 / 迟到桶 / 物化 / 对账 / 审计争用 |
| 空桩 | hold 晋升、迟到补入、分区物化、窗重开 |

## 为何能出多题

开窗关窗、迟到策略、窗重开、部分物化失败回滚、旁路暂存 vs 拒收、争用锁与审计。

## 建窗校验（甲路径，钉死）

口径见 `docs/建窗校验册.md`，落库于 `window_create_registry` 单行（`chosen_path='甲'`，当日触发器禁止改口）：

- 创建前整笔校验：严格 `open_at < close_at`（倒挂/相等拒绝），名称不得重名（禁止覆盖旧世代）。
- 非法即**整笔拒绝、`windows` 零增**，写审计 `window_create_reject`（HTTP 400）。
- 同名并发占建窗锁 `window_create:{name}`：后到只记争用（`contention_log` + `window_create_contention`，HTTP 409），不写业务。
- 校验册偏离（`active=0`/口径非甲）拒写并记 `建窗偏了`（HTTP 409）。
- 不引入 `window_draft`（乙路径废弃，甲乙不混用）；种子装载 `db.seed_if_empty` 复用同一校验函数。
- 校验在服务端写路径钉死，前端提示仅辅助，绕过前端仍被拒绝。

## 互斥切口候选

- 迟到拒收 vs 进迟到桶
- 物化失败整窗回滚 vs 保留成功分区
- 窗重开作废重算 vs 增量补丁合并
- 切版旁路暂存 vs 开始瞬间拒收

## 启动（一键）

```powershell
cd projects/08-windowcut
docker compose up -d --build
```

浏览器：http://127.0.0.1:11021/  
健康检查：GET http://127.0.0.1:11021/api/health

本机开发：

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10605
python -m pytest -q
```
