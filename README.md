# 新能源限发争议

本项目用于整理调度指令、场站可用功率与结算电量之间的时段差异，并保留申诉材料。

项目领域包含调度指令时间区间和申诉状态，供限发事件整理及后续结算沟通使用。

在风光资源充足却发生限发时，调度记录、场站申报和结算侧电量往往相差一个时段，
争议在月末才暴露。本服务把场站、并网点、调度指令、可用功率估计和实际电量关联到
统一时间轴，提供限发事件计算、电量归因、申诉/复核/结算确认状态控制，以及带来源
引用的争议清单。

## 核心设计

**双时间轴（当时可见的信息）**
- 业务时间：事实生效区间（`starts_at`/`ends_at`/`interval_start`…）；
- 事务时间：`recorded_at`，系统得知该事实的时刻，取自注入时钟。
- 所有原始表只追加不更新。任何计算都可指定 `as_of`，按 `recorded_at <= as_of`
  重建“当时可见”的视图 —— 迟到数据、补发指令不会改写历史视图。

**调度指令事件流**：同一 `instruction_id` 的 `issue`（下发/补发）、`correct`
（更正）、`revoke`（撤销）按 `recorded_at` 折叠取最新；补发就是一条
`recorded_at` 晚于作用区间的 `issue` 事件。

**归因瀑布**（每个时段内按序分配，剩余进入下一类）：
1. `equipment_fault` 设备故障 —— 按故障降出力 MW 解释；
2. `dispatch_instruction` 指令限发 —— 按 (可用功率 − 指令上限) 解释；
3. `network_constraint` 网络约束 —— 并网点限额按各场站可用功率占比分摊后解释；
4. `unattributed` 未归因 —— 剩余部分。

**状态控制**
- 案件：`open → appealed → reviewed → settled`；复核结论变更产生新的复核版本
  （`reviewed → reviewed`，结算后也可 `settled → reviewed`）。
- 同一场站的限发区间只允许一个案件（区间重叠即拒绝），防止重复申诉。
- 结算版本：`draft → confirmed`。**confirmed 行不可变**：迟到数据或复核结论变更
  只能 `prepare` 出新的版本号，已确认版本的 `content_hash`、行项目、确认时间不变，
  多个确认版本共存可追溯。

**争议清单**：`GET /disputes` 输出每个案件的立案快照（含 `as_of`）与来源引用
（指令事件、可用功率、结算电量的记录 id 与 `recorded_at`），各方数字可相互对账。

## 运行

```bash
# 启动服务（仅依赖标准库）
PYTHONPATH=src python3 -m curtailment_case --db curtailment.db --port 8080

# 测试（固定时钟 + 临时 SQLite）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tests
```

## HTTP/JSON 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/grid-points` `/plants` | 并网点、场站主数据 |
| POST | `/instructions` | 指令事件：`issue`/`correct`/`revoke`（含补发） |
| POST | `/available-power` `/metered-energy` | 时段数据接入（`records` 批量） |
| POST | `/faults` `/network-constraints` | 设备故障、并网点网络约束 |
| GET | `/curtailment-events?plant_id&start&end[&as_of]` | 限发事件计算（含来源引用） |
| GET | `/dispatch-instructions?plant_id[&as_of]` | 当时可见的有效指令 |
| POST | `/cases` | 立案（固定计算快照；区间重叠返回 409） |
| POST | `/cases/{id}/appeal` `/cases/{id}/review` | 申诉、复核（结论变更产生新版本） |
| POST | `/settlements/prepare` `/settlements/{id}/confirm` | 准备/确认结算版本 |
| GET | `/settlements?plant_id&period` | 结算版本列表（全部历史版本） |
| GET | `/disputes?plant_id&period=YYYY-MM` | 带来源引用的争议清单 |

错误统一为 `{"error": {"code", "message"}}`；时间戳一律 ISO 8601（UTC）。

## 目录结构

```
src/curtailment_case/
  contracts.py   领域契约：CaseState / Attribution / DispatchInterval ...
  timeutil.py    统一时间轴（UTC 秒级 ISO 字符串，字典序即可比较）
  clock.py       SystemClock / FixedClock（测试注入）
  db.py          SQLite schema（只追加原始表 + 不可变结算版本）
  engine.py      归因引擎：as_of 视图重建 + 瀑布分配 + 事件合并
  service.py     业务层：状态机、防重复、结算版本、争议清单
  api.py         HTTP/JSON 路由（标准库 http.server）
  __main__.py    服务入口
tests/
  test_engine.py   跨日区间、指令更正/撤销历史、迟到数据、补发、归因瀑布
  test_service.py  状态机、防重复申诉、已确认结算版本不被改写
  test_api.py      HTTP 全流程与错误分支
```
