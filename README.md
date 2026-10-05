# 退役电芯成组放行 API

用 **Python + FastAPI + SQLite** 实现的退役动力电池电芯成组（串联 PACK）预选与放行服务。

## 功能

- **录入**：一次最多录入 12 节待选电芯（ID、化学体系、容量 Ah、内阻 mΩ、检测版本）；检测版本可随时修订。
- **预选** `POST /preselect`：在指定化学体系的**未占用**电芯中，按
  - 串联节数 3～6 节（`min_size`/`max_size`）
  - 最低单芯容量 `min_capacity_ah`
  - 组内容量差上限 `max_capacity_delta`
  - 组内内阻差上限 `max_resistance_delta`

  枚举所有可行组合，返回**受限容量（组内最小单芯容量）最大**的一组；受限容量相同时按电芯 ID
  序列（排序后）字典序裁决。返回候选 ID、受限容量、组内极差及**各芯检测版本**（确认时回传用）。
  无可行组合时返回明确的失败原因码与说明（容量不达标 / 数量不足 / 容量差超限 /
  内阻差超限 / 两差无法同时满足 / 电芯已全部占用）。
- **确认** `POST /confirm`：在**单个 SQLite 事务**（`BEGIN IMMEDIATE` 写锁）中
  1. 重查每个候选电芯是否存在、是否同体系、**是否仍未占用**、**检测版本是否与预选时一致**；
  2. 重新校核容量/内阻约束（防止预选后数据修订）；
  3. 任一条件不满足则**整组拒绝（HTTP 409）并回滚，绝不留下部分占用**；
  4. 全部通过则原子写入组记录、逐芯检测快照，并把整组电芯标记为占用。
- **不可改写的快照**：组记录与 `group_snapshots` 在数据库层由触发器禁止
  `UPDATE`/`DELETE`；已占用电芯不能被释放。之后即使修订电芯的检测版本/参数，
  已确认组的快照也永不改变，并可按**组编号**查询。

## 一条命令启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 **http://0.0.0.0:8000**（宿主机访问端口 **8000**），数据持久化在命名卷
`battery-data`（容器内 `/data/battery.db`）。

## 本地运行（可选）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：`GET http://localhost:8000/health`

## 可执行调用示例（curl）

### 1. 录入电芯（最多 12 节）

```bash
curl -s -X POST http://localhost:8000/cells \
  -H 'Content-Type: application/json' \
  -d '{
    "cells": [
      {"id":"C01","chemistry":"LFP","capacity_ah":5020,"resistance_mohm":1.05,"test_version":"REV-1"},
      {"id":"C02","chemistry":"LFP","capacity_ah":5015,"resistance_mohm":1.08,"test_version":"REV-1"},
      {"id":"C03","chemistry":"LFP","capacity_ah":5012,"resistance_mohm":1.10,"test_version":"REV-1"},
      {"id":"C04","chemistry":"LFP","capacity_ah":5008,"resistance_mohm":1.12,"test_version":"REV-1"},
      {"id":"C05","chemistry":"LFP","capacity_ah":5010,"resistance_mohm":0.95,"test_version":"REV-1"},
      {"id":"C06","chemistry":"LFP","capacity_ah":5005,"resistance_mohm":1.20,"test_version":"REV-1"},
      {"id":"C07","chemistry":"LFP","capacity_ah":5010,"resistance_mohm":1.00,"test_version":"REV-1"},
      {"id":"C08","chemistry":"LFP","capacity_ah":4990,"resistance_mohm":1.02,"test_version":"REV-1"},
      {"id":"C09","chemistry":"LFP","capacity_ah":4985,"resistance_mohm":1.30,"test_version":"REV-1"},
      {"id":"C10","chemistry":"LFP","capacity_ah":5030,"resistance_mohm":1.50,"test_version":"REV-1"},
      {"id":"C11","chemistry":"LFP","capacity_ah":4960,"resistance_mohm":0.90,"test_version":"REV-1"},
      {"id":"C12","chemistry":"LFP","capacity_ah":4995,"resistance_mohm":1.06,"test_version":"REV-1"}
    ]
  }'
```

> C10 容量最高但内阻离群（1.50 mΩ），C11 容量 4960 Ah 低于后续设的最低容量门槛。

### 2. 预选一组（3～6 串，单芯 ≥4980 Ah，容量差 ≤30，内阻差 ≤0.25 mΩ）

```bash
curl -s -X POST http://localhost:8000/preselect \
  -H 'Content-Type: application/json' \
  -d '{
    "chemistry":"LFP",
    "min_size":3,"max_size":6,
    "min_capacity_ah":4980,
    "max_capacity_delta":30,
    "max_resistance_delta":0.25
  }'
```

返回（受限容量最大 5012 Ah；含 C10 的高容量组合因内阻差 0.45 超限被淘汰）：

```json
{
  "feasible": true,
  "candidate": {
    "candidate_ids": ["C01","C02","C03"],
    "size": 3,
    "limiting_capacity_ah": 5012.0,
    "capacity_spread_ah": 8.0,
    "resistance_spread_mohm": 0.05,
    "versions": {"C01":"REV-1","C02":"REV-1","C03":"REV-1"},
    "constraints": {"chemistry":"LFP","min_size":3,"max_size":6,
                    "min_capacity_ah":4980.0,"max_capacity_delta":30.0,
                    "max_resistance_delta":0.25}
  },
  "pool_size": 12
}
```

### 3. 确认候选（整组原子占用 + 保存快照）

把预选返回的 `versions` 原样回传：

```bash
curl -s -X POST http://localhost:8000/confirm \
  -H 'Content-Type: application/json' \
  -d '{
    "chemistry":"LFP",
    "candidate_ids":["C01","C02","C03"],
    "versions":{"C01":"REV-1","C02":"REV-1","C03":"REV-1"},
    "min_size":3,"max_size":6,
    "min_capacity_ah":4980,
    "max_capacity_delta":30,
    "max_resistance_delta":0.25
  }'
```

```json
{
  "group_no": 1,
  "candidate_ids": ["C01","C02","C03"],
  "limiting_capacity_ah": 5012.0,
  "versions": {"C01":"REV-1","C02":"REV-1","C03":"REV-1"},
  "snapshot_cells": [
    {"position":1,"cell_id":"C01","chemistry":"LFP","capacity_ah":5020.0,"resistance_mohm":1.05,"test_version":"REV-1"},
    {"position":2,"cell_id":"C02","chemistry":"LFP","capacity_ah":5015.0,"resistance_mohm":1.08,"test_version":"REV-1"},
    {"position":3,"cell_id":"C03","chemistry":"LFP","capacity_ah":5012.0,"resistance_mohm":1.10,"test_version":"REV-1"}
  ]
}
```

### 4. 候选过期场景：确认前复测改了检测版本

```bash
# 预选下一组 → 假设候选为 C04/C05/C07；此时 C05 完成复测，版本升到 REV-2
curl -s -X PATCH http://localhost:8000/cells/C05/test-version \
  -H 'Content-Type: application/json' -d '{"test_version":"REV-2"}'

# 仍用预选时的旧版本 REV-1 去确认 → 整组拒绝，无任何占用
curl -i -X POST http://localhost:8000/confirm \
  -H 'Content-Type: application/json' \
  -d '{
    "chemistry":"LFP",
    "candidate_ids":["C04","C05","C07"],
    "versions":{"C04":"REV-1","C05":"REV-1","C07":"REV-1"},
    "min_size":3,"max_size":6,
    "min_capacity_ah":4980,
    "max_capacity_delta":30,
    "max_resistance_delta":0.25
  }'
```

```
HTTP/1.1 409 Conflict
{"detail":{"code":"VERSION_CONFLICT","message":"候选已过期或不再满足放行条件，整组拒绝（未产生任何占用）",
           "version_conflicts":[{"cell_id":"C05","expected":"REV-1","current":"REV-2"}]}}
```

若另一请求已抢先占用候选，则返回 `409` 且 `code` 为 `OCCUPIED`；预选条件不再满足时为
`CONSTRAINT_VIOLATED`。并发确认由 `BEGIN IMMEDIATE` 写锁串行化，失败方整组回滚。

### 5. 无可行组合时的原因说明

```bash
curl -s -X POST http://localhost:8000/preselect \
  -H 'Content-Type: application/json' \
  -d '{"chemistry":"NMC","min_size":3,"max_size":6,"min_capacity_ah":4000,
       "max_capacity_delta":30,"max_resistance_delta":0.25}'
# {"feasible":false,"reason_code":"ALL_OCCUPIED","reason":"该化学体系下没有未占用电芯…","pool_size":0}
```

原因码：`ALL_OCCUPIED` / `NOT_ENOUGH_CELLS` / `MIN_CAPACITY_UNREACHABLE` /
`CAPACITY_SPREAD_EXCEEDED` / `RESISTANCE_SPREAD_EXCEEDED` / `CONSTRAINTS_INCOMPATIBLE`。

### 6. 按组编号查询不可改写的检测快照

```bash
curl -s http://localhost:8000/groups/1
```

即使之后调用 `PATCH /cells/C02/test-version` 修订了 C02，本组快照仍永久保持确认时的
`REV-1`。其他接口：`GET /cells`（可加 `?chemistry=LFP&occupied=false`）、`GET /groups`。

## 数据完整性

- `group_snapshots.cell_id` 上有 `UNIQUE` 约束，数据库层面杜绝同一电芯进入两个组；
- 触发器禁止 `groups` / `group_snapshots` 的 `UPDATE`、`DELETE`，并禁止把已占用电芯改回未占用；
- 确认流程在单事务内完成“重查 → 校验 → 写组 → 写快照 → 占用”，异常即回滚。

## 目录结构

```
app/
  config.py     # 配置（DB_PATH、12 节上限）
  db.py         # SQLite 连接、建表、不可改写触发器
  schemas.py    # Pydantic 请求/响应模型
  grouping.py   # 组合枚举、优选裁决、不可行原因诊断
  main.py       # FastAPI 路由（录入/修订/预选/确认/查询）
Dockerfile  docker-compose.yml  requirements.txt  README.md
```
