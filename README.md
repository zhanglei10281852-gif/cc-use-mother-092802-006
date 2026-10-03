# 新能源限发争议

本项目用于整理调度指令、场站可用功率与结算电量之间的时段差异,并保留申诉材料。

项目领域包含调度指令时间区间和申诉状态,供限发事件整理及后续结算沟通使用。

在风光资源充足却发生限发时,调度记录、场站申报和结算侧电量可能相差一个时段,
争议往往在月末才暴露。本服务把场站、并网点、调度指令、可用功率估计和实际电量
关联到统一时间轴,提供限发事件归因、申诉/复核/结算确认状态控制,以及带来源
引用的争议清单。

## 设计要点

- **统一时间轴**:一切量测与指令对齐到 15 分钟结算时段(`epoch // 900`);
  跨日、跨月事件按时段切分归属到各自结算周期。
- **双时间(bitemporal)**:每条事实带 `recorded_at`(注入时钟)与业务时间。
  指令补发/更正/撤销、表计与可用功率的迟到更正都只追加新版本,从不改写历史;
  `as_of` 查询还原"当时可见的信息"。
- **归因优先级(逐时段)**:设备故障(停机申报) > 指令限发(有效且目标值低于
  可用功率的调度指令) > 网络约束(剩余)。停机与指令重叠时争议清单会提示
  归因冲突,待人工确认。
- **申诉状态机**:`submitted → under_review → accepted/rejected → settled`;
  `withdrawn` 仅允许从 submitted/under_review 进入。同一场站、区间重叠且仍在
  进行中的申诉会被拒绝(`duplicate_appeal`),避免同一限发区间重复申诉。
- **结算版本不可变**:确认即生成带 digest 的快照;迟到数据或复核结论变更不会
  改写已确认版本,只会在下一次确认时产生 version_no+1,差异由争议清单显式呈现。

## 争议类型(`GET /disputes`)

| kind | 含义 |
| --- | --- |
| `unappealed_curtailment` | 限发事件尚无进行中的申诉 |
| `late_data` | 迟到/补发数据(记录时间晚于事件结束,含月末结账后到达) |
| `boundary_shift` | 指令边界与实测限发起点相差若干时段(如相差一个时段) |
| `attribution_conflict` | 停机申报与受限指令区间重叠,归因待确认 |
| `energy_mismatch` | 指令隐含限发量与表计损失对不上 |
| `review_changed_after_confirm` | 复核结论在结算确认后变更 |
| `settlement_stale` | 已确认版本与当前计算结果不一致 |

每条争议附 `sources` 来源引用(指令/表计/可用功率/停机申报/申诉/复核/结算版本,
含标识与记录时间),可回溯各方"当时说了什么"。

## 运行

```bash
# 测试(固定时钟 + 内存 SQLite)
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tests

# 演示:跨日限发 -> 申诉 -> 结算确认 -> 迟到数据 -> 争议清单
python3 demo.py

# 启动 HTTP 服务
PYTHONPATH=src python3 -m curtailment_case.api --host 127.0.0.1 --port 8080 --db case.db
```

## HTTP/JSON 接口

所有时间字段为带时区的 ISO-8601 字符串(查询参数中建议用 `Z` 或对 `+` 编码)。

```
POST /grid-points {name, id?}
POST /plants {name, grid_point_id, capacity_mw, id?}
POST /instructions {instruction_id, plant_id, target_mw, start, end, issued_at, source?}
POST /instructions/{instruction_id}/revoke          # 撤销(追加 revoked 版本)
GET  /instructions?plant_id[&as_of]                 # as_of 还原当时可见的指令
GET  /instructions/{instruction_id}/history         # 全部版本
POST /available-power {plant_id, source?, points:[{ts, mw}]}
POST /meter-readings {plant_id, source?, readings:[{start, end, kwh}]}
POST /outages {plant_id, start, end, reason?, source?}
GET  /events?plant_id&start&end[&as_of]             # 限发事件与归因
POST /appeals {plant_id, start, end, reason, attribution?}
GET  /appeals[?plant_id]
POST /appeals/{id}/start-review | /conclude | /withdraw
POST /settlements/confirm {plant_id, period}        # period 为 'YYYY-MM'
GET  /settlements?plant_id&period[&version]
GET  /disputes?plant_id&period                      # 带来源引用的争议清单
```

错误统一为 `{"error": {"code", "message"}}`;业务冲突(如 `duplicate_appeal`、
`invalid_transition`)返回 409,参数问题 400,不存在 404。

## 目录结构

```
src/curtailment_case/
  contracts.py   # 既有契约(CaseState / DispatchInterval)
  timeutil.py    # 统一时间轴:15 分钟时段、结算周期
  clock.py       # 时钟抽象与固定时钟
  store.py       # SQLite 模式(只追加的事实表 + 不可变结算版本)
  service.py     # 领域服务:事件归因、申诉状态机、结算版本、争议清单
  api.py         # 标准库 HTTP/JSON 接口
tests/           # unittest:跨日区间、迟到数据、复核变更、去重、as_of、状态机
demo.py          # 端到端演示
```
