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

代码、离线测试、独立评审、部署和生产 Chrome 验收均通过。

### 离线执行结果

- X Auto/bridge/UI：150 项通过，1 项仅因 Windows 平台跳过。
- 既有 X 发布与账号：236/236；现有权限/UI：61/61。
- 素材/剧集/媒体链路：129/129；catch-up/schedule 恢复链路：138 项通过，1 项仅因 Windows 平台跳过。
- Python 编译、4 个 X Auto JavaScript 语法检查、`git diff --check` 均通过；独立代码评审结论为无 blocker。
- 所有离线 X HTTP 写入均为 mock/fake；未连接真实 X 发布接口。

### 生产验收结果

- 生产精确 release 的聚焦套件 123/123 通过，三个服务和六个 timer/path active；GET 与页面初始读取没有触发新 Token 轮换。
- 部署前六个已批准账号已由既有账号生命周期于 11:30 CST 恢复 active；Chrome 显示六个可发布账号，并实际选中 `1,5,13,14,15,16`，摘要为“已选 6 个”。因此新按钮在本时点正确为零刷新候选，没有人为重复刷新。
- 未批准账号仍显示“未批准发布”并置灰；离线用例证明其在 X 网络调用前被拒绝，临时错误保留 `refresh_required`，模板与最终发布严格校验不放宽。
- 未保存模板、未创建 run/task/queue/log/Post，三道 gate 均 false；最终现有 X 账本 `182/182/181/1`、active/unknown `0/0`，X Auto 七类业务表全为 0。
- 部署前后 Token 文件哈希相同；两份 SQLite `quick_check=ok`。自然 manual `no_pending`、schedule `no_due`、claim 0，X Auto scheduler/runner succeeded。

## BUG-005 手动执行就绪修复（2026-08-12）

- 生产取证确认两次 409 均在门禁检查处结束，X Auto run/task/ledger 为 0，
  没有产生 X 写入。
- 账号 `1` 为 `active + publish_approved`、权限完整、Token 文件 `0600` 且归属正确；
  7 个完整北京时间日指标代均为 `ready`；真实 `gy.g2flow.com` 短链返回
  200、`Cache-Control: no-store`。
- 原只读预览成功但误选 270 秒素材；修复后标准账号有效选择上限为 140 秒，
  token 确认会员仍保留模板 600 秒硬上限。
- 素材池、账号授权页、账号列表和 X Auto 模板账号选择器统一为：会员只显示会员
  类型，无会员/未知账号显示“最长 140 秒”；静态契约用例防止文案回退。
- 本地 X Auto/bridge/UI/app-contract 聚焦套件 159 项执行、158 项通过、
  1 项仅因 Windows 平台跳过；新增账号时长测试全部通过。
- GitHub exact commit 与生产 release 均为
  `0a27b66ff9651d665a19675ac01c8e6c44713283`；服务器不可变 release 再跑
  159/159 通过。回滚包为
  `/mnt/data-disk/x-post-automation/backups/20260812T180553+0800-x-auto-run-now-readiness-0a27b66`，
  manifest 复核通过，两份 SQLite online backup 均 `quick_check=ok`。
- `/health` 回读三门禁与 `is_open` 全为 true。只读 preview 返回 200、
  `reserved=false`，账号 `1` 选中素材 `6120551`、时长 92 秒，不再选择原 270 秒
  候选；未调用 `run-now` 或 publish/canary。
- 模板 `1` 仍为停用；恢复 timer 后 scheduler/runner 于 18:09、18:10 自然执行成功，
  X Auto run/task/ledger 继续为 `0/0/0`。X Sidecar 未重启，Token 组合哈希未变化，
  服务日志无 warning..alert。
- 两份静态根目录与 release 哈希一致，七个公开页面/资产均返回 200，HTML 为
  `Cache-Control: no-store, max-age=0`，cache-buster 为 `20260812run1`。
- 最终既有 X 账本保持 queue/log/published `187/187/186`、unknown/active manual
  `0/0`，最新既有 Post ID 仍为 `2087476109495386331`；本次部署与验收新增 X Post
  为 0。

## BUG-006 ffprobe 依赖缺失（2026-08-12）

- 操作员 Run `1` 于 18:14:35 创建，18:15:16 明确失败。任务错误为
  `media_probe_failed`：进程回退到不存在的 `/usr/bin/ffprobe`。
- 事件链为 material `6120551` 临时预留 → canonical auto-template run `9` 创建 →
  媒体探测失败 → 临时预留释放 → task/run 失败。canonical run `9` 为
  `failed_preflight`、queued/published/unknown 均为 0；没有 queue/log/Post 或模糊写入。
- 生产已有 `/mnt/data-disk/x-post-automation/bin/ffprobe`，SHA256 为
  `4f231a1960d83e403d08f7971e271707bec278a9ae18e21b8b5b03186668450d`，
  `x-post-daily` 用户和同等 systemd 沙箱均可执行。遗漏只发生在 X Auto 独立环境。
- 修复为独立环境显式设置该路径，并由 unit `ExecStartPre` 在 sidecar 启动前验证；
  不修改或重放 Run `1`。部署结果与零额外 Post 复核在发布后补充。
