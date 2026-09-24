# 灾后多制式通信恢复指挥服务

沿海省应急通信后台：汇聚台风过境后乱序、重复、迟到的基站退服、光缆中断、
卫星窗口、便携站库存、抢修队位置与灾情上报，按医院 / 避难点 / 普通区域的
保障规则形成可追溯的恢复方案；在链路退化或更高优先级事件到来时执行**有底线
约束的抢占与重排**；值班员经授权后批准、冻结、恢复、撤回或接续方案。每次决策
保留输入事件版本、规则命中、被替代 / 抢占关系与操作人；服务重启后从只追加
事件日志恢复未完成行动。

## 设计要点

- **事件溯源**：所有输入事件与方案决策写入只追加 JSONL 日志（`data/command_log.jsonl`）。
  重启时按序重放，完整恢复方案谱系与未闭环行动。
- **可重复 / 迟到上报**：值类型上报（灾情、库存、窗口、退化系数、队伍位置）以
  `occurred_at` 最新者为准；退服基站 / 中断光缆按资源记录最新状态，迟到的旧
  “恢复”或旧“退服”不会颠倒现状。
- **方案版本与抢占时机**：重排只生成 `proposed` 草案，不动用现役资源；批准时
  抢占才**原子生效**，旧版同时被接续为 `superseded`。未批准的旧草案在再次
  重排时标记为 `discarded`。
- **有约束抢占**：只能从更低优先级区域回收卫星带宽 / 便携站 / 抢修队，且不得
  把受害方有效保障压到其底线（医院 30Mbps、避难点 10Mbps、普通区 0）。
- **冻结保护**：`frozen` 方案的占用不参与重排；若一份草案批准时其抢占目标已
  被冻结或资源已变，批准被拒绝并要求重新编排。
- **可追溯**：每条决策含 `basis_event_ids`（输入版本）、`rule_hits`（规则命中
  与数值解释）、`supersedes` / `preempted_by`（替代关系）、`operator`（操作人）。
- **可复现**：时钟（`Clock`）与标识生成器（`IdGenerator`）为可注入端口；
  测试与 CLI 演练使用固定时钟和确定性 ID，同一组乱序事件稳定重放出一致结果。

## 目录结构

```
resilience_command/
  clock.py        # 时间端口：SystemClock / FixedClock / ScriptedClock
  identifiers.py  # 标识端口：UuidIds / DeterministicIds
  models.py       # 事件、方案、行动、状态机
  rules.py        # 事件折叠 + 保障规则 + 有约束抢占求解（纯函数）
  auth.py         # 值班员授权（角色能力矩阵，可替换）
  store.py        # 只追加 JSONL 存储 / 内存存储
  service.py      # CommandCenter：接入、重排、授权命令、重放恢复
  api.py          # 标准库 http.server 本地 HTTP 接口
  cli.py          # demo / replay / serve 命令行入口
tests/            # unittest：规则、服务生命周期、HTTP、确定性
```

## 运行

仅依赖 Python 3.11 标准库。

```bash
# 命令行台风场景演练（固定时钟，确定性输出，含重启恢复与乱序自检）
python3 -m resilience_command.cli demo --reset

# 从日志恢复并打印当前方案 / 未完成行动
python3 -m resilience_command.cli replay

# 启动本地 HTTP 服务
python3 -m resilience_command.cli serve --host 127.0.0.1 --port 8080
# 或
python3 -m resilience_command.api --port 8080
```

## HTTP 接口

值班员操作需携带 `X-Operator-Token` 头。本地默认名册（可用 `RC_ROSTER_FILE`
覆盖，见 `auth.py`）：

| 令牌 | 操作人 | 可执行 |
| --- | --- | --- |
| `director-token` | 张值班长 | 批准 / 冻结 / 恢复 / 撤回 / 完成 / 重排 |
| `deputy-token` | 李副班 | 批准 / 冻结 / 恢复 / 撤回 / 重排 |
| `operator-token` | 王值班员 | 重排 / 行动回报 |

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/events` | 接收单条上报（可带 `event_id` 幂等去重） |
| POST | `/events/batch` | 批量接收 `{"events": [...]}` |
| POST | `/replan` | 依据全部已接收事件重排，生成待批方案 |
| GET | `/situation` | 当前折叠态势与资源台账 |
| GET | `/plans?area=` | 方案列表（含全部历史版本） |
| GET | `/plans/{id}` | 单方案详情（规则命中、行动、替代链） |
| POST | `/plans/{id}/approve` | 授权（抢占原子生效、旧版接续） |
| POST | `/plans/{id}/freeze` | 冻结（占用受保护） |
| POST | `/plans/{id}/resume` | 解除冻结 |
| POST | `/plans/{id}/withdraw` | 撤回（行动取消、资源释放） |
| POST | `/plans/{id}/complete` | 整版完成 |
| POST | `/actions/{id}/complete` | 单项行动回报完成 |
| GET | `/actions/pending` | 未闭环行动（重启恢复核对） |
| GET | `/events` / `/decisions` | 输入事件 / 决策审计记录 |

上报事件类型：`base_station_down`、`cable_cut`、`satellite_window`、
`portable_station_stock`、`repair_team_report`、`disaster_report`、
`link_degradation`、`recovery_report`。字段校验见 `service.py`。

### 快速演练

```bash
curl -X POST localhost:8080/events/batch -H 'Content-Type: application/json' \
  -d '{"events":[{"event_id":"e1","type":"satellite_window",
  "occurred_at":"2026-09-24T09:00:00Z","source":"网管","payload":{
  "window_id":"w1","start":"2026-09-24T09:00:00Z",
  "end":"2026-09-24T18:00:00Z","capacity_mbps":50}}]}'

curl -X POST localhost:8080/replan -H 'X-Operator-Token: operator-token' \
  -H 'Content-Type: application/json' -d '{"reason":"首轮编排"}'

curl -X POST localhost:8080/plans/<plan_id>/approve \
  -H 'X-Operator-Token: director-token' -H 'Content-Type: application/json' \
  -d '{"reason":"值班长授权"}'
```

## 保障规则（`rules.py`）

目标有效带宽 = `基础 + 每退服基站增量×N + 每中断光缆增量×N`，再乘覆盖比例：

| 区域 | 基础 | /退服基站 | /中断光缆 | 覆盖比例 | 抢占底线 |
| --- | --- | --- | --- | --- | --- |
| 医院 | 30 | 20 | 10 | 100% | 30 Mbps |
| 避难点 | 10 | 10 | 5 | 80% | 10 Mbps |
| 普通区 | 5 | 5 | 2 | 50% | 0 Mbps |

优先级 = 区域类型分（1000/500/100）+ 灾情等级分（400/250/100/30）
+ 人口分（每百人 1，封顶 200）。同分时按区域名决胜，保证确定性。
卫星窗口按名义 Mbps 记账，有效带宽受全局退化系数折减；便携站每台提供
10Mbps 有效带宽，用于补足抢占 / 退化后的缺口。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q resilience_command tests
```

测试覆盖：乱序 / 重复 / 迟到折叠、恢复事件时间判定、优先级与底线约束抢占、
冻结拦截、撤回释放、版本接续、待批草案丢弃、授权矩阵、幂等接入、日志重启
恢复（内存态与重放态逐字节比对）、真实 HTTP 往返、以及多组乱序接入的决策
一致性。

## 工程约定

领域模型、规则求解、应用服务、持久化与接口层边界清晰；时间、标识与外部
观测通过可替换端口接入。运行数据不写入源码目录（`data/` 已被忽略）。
