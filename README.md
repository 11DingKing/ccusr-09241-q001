# 灾后多制式通信恢复指挥服务

汇聚灾情与通信资源，支撑恢复方案编排和过程追溯。面向台风过境后的应急通信值班场景：
接收可重复、可迟到的基站退服/光缆中断/卫星窗口/便携站库存/抢修队位置上报，
按医院、避难点、普通区域的保障规则自动生成可追溯的恢复方案，在链路退化或更高
优先级事件到来时执行有约束的抢占与重排，并支持值班员经授权后冻结、撤回、接续方案。

## 架构

```
resilience_command/
├── domain/            领域层（纯函数，无外部依赖）
│   ├── models.py      上报、事实、需求、分配、方案、决策
│   ├── rules.py       保障规则编号与带宽参数
│   └── planner.py     规划器：事实集合 -> 各区域目标分配
├── application/
│   ├── ports.py       可替换端口：时钟 / 事件日志 / 标识生成
│   └── service.py     指挥服务：接收上报、重排、值班员操作、审计
├── infrastructure/
│   ├── clock.py       系统时钟 / 可手动推进的演练时钟
│   ├── eventlog.py    JSONL 事件日志（追加写、全量重放）
│   ├── ids.py         单调标识生成（重放后编号连续）
│   └── auth.py        操作人令牌与角色权限
└── interfaces/
    ├── http_api.py    本地 HTTP 接口（仅标准库）
    └── cli.py         serve / drill / replay 命令行入口
```

### 关键机制

- **事件溯源**：所有被受理的上报（`REPORT`）与决策（`DECISION`）追加写入
  JSONL 事件日志；服务重启时全量重放折叠出内存状态，未完成行动
  （ACTIVE/FROZEN 方案下的行动）原样恢复，方案/决策编号连续。
- **幂等与乱序**：上报以 `event_id` 去重；同一来源（如 `satwin:SAT-1`）的多份
  上报按 `(occurred_at, event_id)` 取最新，因此迟到与乱序上报收敛到一致事实，
  与到达顺序无关。
- **确定性重规划**：每次变更都基于当前事实集合全量重算目标分配，再与现行方案
  比对落账。规划器是纯函数，时钟、标识均可注入，同一组事件必然重放出一致结果。
- **有约束的抢占**：按 医院 > 避难点 > 普通区域 的顺序贪心分配稀缺资源；
  被冻结方案锁定的资源在分配前预先保留，任何自动重排都无法抢占；
  同优先级先到先得。重排原因（`PREEMPTED` / `RESOURCE_DEGRADED` / …）写入决策。
- **可追溯**：每条决策记录输入版本（各来源采纳的 `event_id`）、命中规则
  （`R-HOSPITAL-GUARANTEE` 等）、被替代方案（`replaces`）与操作人
  （`system` 或值班员姓名）。

## 运行

```bash
# 启动本地 HTTP 服务（事件日志落盘，令牌来自文件或 RESILIENCE_OPERATOR_TOKENS）
python3 -m resilience_command serve --port 8080 --db var/eventlog.jsonl --tokens ops.json

# 命令行演练：手动时钟按场景推进，同一场景输出逐字节一致
python3 -m resilience_command drill scenarios/typhoon_landing.json --db var/drill.jsonl

# 模拟重启：从事件日志重放恢复未完成行动
python3 -m resilience_command replay --db var/drill.jsonl
```

`ops.json` 形如：`{"tokens": {"tok-duty": {"name": "林值班", "role": "duty_officer"}}}`。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/health` | 健康检查 |
| POST | `/v1/reports` | 接收上报（单条或 `{"reports": [...]}` 批量），重复 `event_id` 幂等忽略 |
| GET | `/v1/state` | 值班大屏快照：区域需求、资源余量、未完成行动 |
| GET | `/v1/plans?status=&area_id=` | 方案列表 |
| GET | `/v1/plans/{id}` | 方案详情（行动、规则、输入版本、替代关系） |
| POST | `/v1/plans/{id}/freeze` | 冻结方案 `{"token": ...}` |
| POST | `/v1/plans/{id}/withdraw` | 撤回方案并挂起区域 `{"token": ..., "reason": ...}` |
| POST | `/v1/plans/{id}/resume` | 接续被冻结的方案 |
| POST | `/v1/areas/{id}/resume` | 接续区域，恢复自动编排 |
| POST | `/v1/reevaluate` | 按当前时钟重估（如卫星窗口过期） |
| GET | `/v1/decisions?plan_id=&area_id=` | 决策审计 |

上报类型：`station_status`（基站退服/恢复）、`fiber_status`（光缆中断/恢复）、
`satellite_window`（卫星链路可用窗口）、`portable_inventory`（便携站库存）、
`repair_team`（抢修队位置）。区域类别接受 `医院` / `避难点` / `普通区域`
（或 `HOSPITAL` / `SHELTER` / `ORDINARY`）。

## 演练场景

`scenarios/typhoon_landing.json` 覆盖完整故事线：资源入库 → 普通区域退服 →
避难点光缆中断 → 医院退服触发抢占 → 重复上报幂等 → 卫星链路退化重排 →
迟到上报归位 → 冻结医院方案 → 新窗口补足 → 撤回/接续 → 恢复闭环。
场景步骤支持 `report(s)` / `freeze` / `withdraw` / `resume_plan` /
`resume_area` / `reevaluate`，时间由场景内 `at` 字段驱动。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试通过 `ManualClock` 注入时间，覆盖：规划规则、幂等与乱序收敛、抢占约束、
授权操作、重启恢复、排列收敛与演练逐字节重放、HTTP 接口。

## 编译检查

```bash
python3 -m compileall -q resilience_command tests
```

## 工程约定

项目采用 Python 包目录组织服务端代码。领域模型、应用服务、持久化适配和接口层
保持边界清晰；时间、标识生成及外部观测均通过可替换端口接入，便于稳定复现业务
过程。运行数据（事件日志等）写入 `var/`，不得写入源码目录。
