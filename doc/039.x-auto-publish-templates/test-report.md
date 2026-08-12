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

## 2026-08-12 Chrome 实测增量

### 结论

Chrome 生产实测发现的 CSS 漏发、静态旧缓存、UI DTO 映射和共享 flock 目录竞态均已修复并部署。两个页面最终验收通过；本次 X Auto 部署与验收未创建模板或运行，也未额外触发 X Post。期间账本增加的 5 条发布事实来自既有 X 自然 schedule。

### 执行证据

- 本地：完整 X auto/admin UI/app-contract 聚焦套件 129 项通过、1 项因 Windows 跳过；既有 X bridge/manual/daily/schedule/catchup/account 281/281 通过；Python 编译与 JavaScript 语法检查通过。
- 服务器最终 release：135/135 通过；`nginx -t` 通过。
- Chrome：模板页与运行页加载的目标 X Auto 样式表包含 139 条 CSS 规则，登录/权限门隐藏；统计为 0，刷新/筛选/重置正常；新建页标题和运行 404 中文错误正确。
- 生产自然运行：10:31–10:45 的 X auto scheduler/runner 全部 succeeded，既有 manual 持续 `no_pending`、schedule 持续 `no_due`；共享目录 inode 不变。
- 最终状态：现有 queue/log/published/failed 为 `182/182/181/1`，active/unknown 为 `0/0`；X auto 七类业务表均为 0；Token 哈希不变。

### 发布结论

生产 release `c4bc4e70adf926f2e58fa70d9af86dd03ff63ff7` 可保留。三道 gate 继续全关，当前 15 个账号均为 `refresh_required` 并在编辑器中置灰；账号刷新与真实模板启用仍需另行授权。

## BUG-004 账号资格刷新增量

### 当前结论

代码、离线测试与独立评审通过；部署和生产 Chrome 验收尚未完成。本节只记录已经执行的离线事实。

### 离线执行结果

- X Auto/bridge/UI：150 项通过，1 项仅因 Windows 平台跳过。
- 既有 X 发布与账号：236/236；现有权限/UI：61/61。
- 素材/剧集/媒体链路：129/129；catch-up/schedule 恢复链路：138 项通过，1 项仅因 Windows 平台跳过。
- Python 编译、4 个 X Auto JavaScript 语法检查、`git diff --check` 均通过；独立代码评审结论为无 blocker。
- 所有离线 X HTTP 写入均为 mock/fake；未连接真实 X 发布接口。

### 生产待验证项目

- GET 账号列表零 X 调用、零 Token 写入。
- 仅有模板导航权限的登录操作员可触发，页面只对 `refresh_required + publish_approved=true` 账号逐个串行刷新；成功后回读 `active + approved + publish_eligible` 才可选择。
- 未批准/非目标状态失败关闭；临时错误保持可重试，明确撤销要求重新授权。
- 创建、编辑、启用、执行与最终发布严格校验不放宽。
- 验收不创建模板/run/task/queue/log/Post，三道 gate 与既有发布账本保持不变。
