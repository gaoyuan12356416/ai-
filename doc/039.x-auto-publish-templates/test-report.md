# 测试报告

## 测试结论

离线/集成和“闸门全关”的生产验收均通过。测试与生产验收都没有创建真实 X Post。

## 测试范围

- 模板/版本、固定与随机计划、指标缓存、两层选材、600 秒边界和账号串行。
- provisional/canonical 两阶段素材占用、全局 X queue 去重、Premium token-scoped 路由。
- manual 与 auto 来源隔离、精确 recover、unknown outcome、防二发、闭门对账。
- 主 API Cookie/权限/同源/审计、响应脱敏、页面导航和编辑器宏。
- 既有 X manual/daily/catchup/schedule/material/drama/account，以及 TT 自动模板回归。

## 执行统计

| 套件 | 执行 | 通过 | 失败 | 阻塞 |
| --- | ---: | ---: | ---: | ---: |
| X auto + bridge + UI/代理 + TT 兼容 | 162 | 162 | 0 | 0 |
| 既有 X 发布与账号回归 | 244 | 244 | 0 | 0 |
| X accounts app contract | 28 | 28 | 0 | 0 |
| 合计执行次数 | 434 | 434 | 0 | 0 |

上述分套件统计保留开发阶段的互斥分组。合并最新生产基线后另做一次单命令最终门禁，`425/425` 通过；服务器不可变 release 上的部署/校验/bridge 套件另为 `29/29` 通过，两者与上表有重叠，不重复求和。

## 缺陷情况

评审发现的 1 个 P0、6 个 P1 均已修复并补回归；无未关闭 blocker。基线中按旧源码字符串断言账号 affinity 的测试已改为验证真实 `settings.account_ids` 与 `eligible_account_ids` 契约，未改变生产逻辑。

## 验证证据

- canonical `failed_preflight` 会释放未入队素材并释放 active account；记录失败不可用时保留 retry_wait。
- publish transport unknown 首次持久化，后续只 query/recover；canonical published/failed 均能终态且 publish 调用保持 1 次。
- recover 对 queued/no-log、queued/reserved、publishing、lock busy 和迟到 publish 竞态均有测试。
- live gates 全关时 pending 不领取；已有 ready/unqueued run 只对账/终止，publish 调用为 0。
- 部署静态契约验证独立端口/SQLite/token、共享 `/run/x-post-daily/runner.lock`、三 gate=0 和非持久 timer。
- 生产迁移前后旧表旧列内容哈希一致；现有 queue/log/confirmed Post/active/unknown 为 `177/177/176/0/0`，账号 token 目录哈希不变。
- 生产自然 scheduler/runner 执行成功且 x_auto template/run/task/ledger/event 仍全为 0；现有 schedule/manual 持续 `no_due/no_pending`。

## 遗留风险

- 首次真正启用模板前仍需运营确认账号范围、短链域名和真实发布量；本次不授权启用。
- 生产服务已从已推送的复合 release `0e03210` 构建，并按“先 provision 新 bearer、再重启既有 X sidecar”的顺序完成。
- 未用真实 Post 做 canary；生产验收依靠健康、自然 held/no_due、账本不变和现有计时器证据。

## 发布建议

保持当前三 gate 全关、模板为空的交付状态。任何真实模板启用须另行授权；若未来存在 auto active/unknown row，不得直接回滚为不识别来源的旧 sidecar。
