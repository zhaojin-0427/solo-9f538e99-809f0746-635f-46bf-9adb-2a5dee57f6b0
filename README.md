# 公交接续等待复盘服务

一班公交晚点、前序车实发漏记时，复盘员用本服务回答两个问题：
**车辆接续最少拖了多久、最多能解释几分钟。**

FastAPI + SQLite 单容器服务，`docker compose up` 即可启动，打开 `/docs` 交互导入计划、补录实发、查询区间。

## 快速开始

```bash
docker compose up --build        # 服务监听 8000 端口，数据落在命名卷 busdata
# 或本地运行
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

打开 http://localhost:8000/docs 即可操作全部接口。

```bash
pip install -r requirements-dev.txt && python -m pytest   # 运行测试（44 个用例）
```

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/days/{day}/plan` | 按运营日一次导入计划表（班次计划分钟 + 接续），版号为 1 |
| POST | `/days/{day}/plan/replan` | 带当前版号重发全量计划，并按新旧班号对照迁移实发 |
| DELETE | `/days/{day}` | 删除运营日（用于重导计划） |
| POST | `/days/{day}/actuals` | 成批补录 / 更正 / 撤销实发（`actual: null` 即撤销） |
| GET | `/days/{day}/trips/{trip_id}` | 单班查询：计划版号 + 实发 + 接续等待区间 |
| GET | `/days/{day}/actuals` | 当日全部实发（含已知等待，分页） |
| GET | `/days`、`/days/{day}` | 运营日概览（含当前版号） |

分钟均指"运营日零点起算的分钟"，取值 `[0, DAY_HORIZON_MINUTES)`，默认 2880（48 小时，可配）。

### 导入计划（POST /days/{day}/plan）

```json
{
  "trips": [{"trip_id": "A101", "planned": 480}, {"trip_id": "B201", "planned": 500}],
  "connections": [{"pred": "A101", "succ": "B201", "min_interval": 15}]
}
```

校验：每班至多 4 条前序、接续无环（Kahn 拓扑）、分钟不越界、计划在运营日时长内整体可行；
任一不满足则 400/409 且不写入任何数据。同一运营日重复导入返回 409，需先 DELETE。
初次导入的计划版号为 `1`。

### 计划换版（POST /days/{day}/plan/replan）

排班重发某日全量计划时，复盘员带着**所见版号**提交新版班次、接续表和新旧班号一对一
对照；已提交实发只按对照迁移（允许两班互换编号，不得因同号自动继承）：

```json
{
  "version": 1,
  "trips": [{"trip_id": "A101", "planned": 480}, {"trip_id": "B202", "planned": 500}],
  "connections": [{"pred": "A101", "succ": "B202", "min_interval": 15}],
  "mapping": [{"new_trip_id": "B202", "old_trip_id": "B201"}]
}
```

- `version` 必须是库中当前版号；过期版号返回 409 并给出 `current_version`，不改动数据。
- `mapping` 为 `new -> old`，新旧两侧各自不得重复；须覆盖当日所有已有实发的旧班次。
  旧班号不存在于旧计划、或已有实发找不到去向，均拒绝（400/409）。
- 新计划沿用原校验规则（≤4 前序、无环、不越界）；迁入实发同样沿用原规则
  （不得早于新版计划、不得越界）并做全日可行性校验，失败整体回滚，计划与实发原样保留。
- **与补录交错**：验算在写锁之外用读快照进行，期间补录/更正/撤销仍可提交；真正切换时
  在写锁内重读实发，若验算期间有变化则按**当时最新已提交的实发**重新验算再迁移。
- 切换成功版号 +1；写入失败返回 503，已提交的计划与实发保持不变。

### 补录实发（POST /days/{day}/actuals）

```json
{"version": 2, "updates": [{"trip_id": "A101", "actual": 490}, {"trip_id": "B201", "actual": null}]}
```

- 实发不得早于计划、不得越界；批次内班次不得重复（400）。
- **版号**：首次换版前（版号 1）可省略 `version`，沿用原批录格式；换版后批次必须带当前
  版号，旧版或缺失一律 409 拒绝，未写入任何更改。
- 整批写入后做全日可行性校验：凡"实发 < 所需最早时刻"或"未知班次被已知实发逼到
  运营日之外"，整批回滚（保留原实发与区间），返回 409 并指出**冲突时刻与接续链**：

```json
{"detail": {"message": "批次与已知实发矛盾，未写入任何更改", "total_conflicts": 1,
  "conflicts": [{"kind": "actual_before_required", "trip_id": "B201",
    "actual": 500, "required_earliest": 505,
    "chain": [{"trip_id": "A101", "planned": 480, "actual": 490},
              {"trip_id": "B201", "planned": 500, "actual": 500}],
    "edges": [{"pred": "A101", "succ": "B201", "min_interval": 15}]}]}}
```

### 单班查询（GET /days/{day}/trips/{trip_id}）

```json
{"day": "2026-11-21", "version": 2, "trip_id": "B201", "planned": 500, "actual": 510,
 "wait": {"known_wait": 5, "min_wait": 5, "max_wait": 5}}
```

- `version`：本次查询所见的计划版号，与 `actual`、`wait` 取自同一读事务快照；
  换版与补录交错时不会出现版号、实发、区间互相错配。
- `known_wait`：各前序**实发** + 各自最短间隔，最晚时刻超出本班计划的分钟数；无前序或未超出为 0。
- `[min_wait, max_wait]`：在所有满足"计划、接续、已知实发"的补全中能达到的最小/最大等待。
  未知前序取最早可行时刻（沿祖先传播 `max(计划, 前序EF+间隔)`）得下限，
  取最晚可行时刻（沿子孙传播 `min(后继LF-间隔, 末日分钟)`，已知实发固定）得上限。
- 更正/撤销另一班会改变共用前序的可行时刻，本班区间在下次查询时自然反映（按查询实时计算）。
- 无实发的班次 `actual` 与 `wait` 为 `null`。

## 设计要点

- **一致性**：所有写操作在 `BEGIN IMMEDIATE` 事务内"逐条校验 → 写入 → 全日 EF/LF 可行性
  校验"，失败整体回滚；因此库中数据始终存在合法补全，区间恒有 `min <= max`。
- **换版原子性**：换版验算在写锁外（读快照）进行，切换在单个写事务内完成
  "校验版号 → 重读最新实发 → 必要时重验 → 整日替换计划 → 版号 +1"；
  任何失败（版号过期、实发无去向、校验不通过、写入失败）都不改动已提交的计划与实发。
- **旧库兼容**：启动时若 `days` 表无 `version` 列则自动补上，已有计划即为初始版号 1。
- **同一次提交**：单班查询在一个读事务快照内取计划版号、实发并算区间，换版/补录期间
  查询不受写事务影响（WAL 模式，读写不互斥）。
- **性能**：单日 5 万班次可导入（拓扑序在导入时算好入库）。单班查询只走该班前序的
  祖先/子孙子图（按需取数、迭代遍历不爆栈），不遍历全日接续；批次校验为全日
  O(班次+接续) 传播，5 万规模亚秒级。
- **持久化**：SQLite 文件（Docker 中为 `/data` 卷），重启后仍可查。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DB_PATH` | `/data/app.db`（容器）/ `./data/app.db`（本地） | SQLite 文件路径 |
| `DAY_HORIZON_MINUTES` | `2880` | 运营日分钟数上界（不含） |

## 目录结构

```
app/
  main.py   # FastAPI 接口与请求/响应模型
  logic.py  # 拓扑排序、EF/LF 可行时刻、等待区间、可行性校验与冲突链
  db.py     # SQLite 连接、模式、读写事务
tests/test_app.py
Dockerfile / docker-compose.yml
```
