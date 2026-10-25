# Meta 违规素材定时清理维护

脚本运行在 CPU `43.166.187.96`，北京时间每天 `09:00`、`21:00` 扫描并清理。
产品与规则的完整约定见
[维护 Skill](skills/meta-violation-material-cleanup/SKILL.md)。本目录不保存任何密钥。

## 文件和配置

| 项目 | 路径 |
| --- | --- |
| 运行根目录 | `/mnt/data-disk/meta-violation-material-cleanup` |
| 不可变 Git release | `/mnt/data-disk/meta-violation-material-cleanup/releases/<sha>` |
| 当前 repo 根目录链接 | `/mnt/data-disk/meta-violation-material-cleanup/current` |
| CLI | `current/server-monitors/meta-violation-material-cleanup/run.py` |
| 配置 | `/mnt/data-disk/meta-violation-material-cleanup/config.json` |
| 共享删除账本 | `/mnt/data-disk/fb-ad-asset-delete/tasks.sqlite3` |
| 现有业务连接配置 | `/root/drama_material_service/.env` |
| 现有郜远飞书接收配置 | `/root/codex_test/fb_2446289_feishu_config.json` |
| 2446289 旧清理互斥锁 | `/tmp/fb_2446289_daily_replace_delete.lock` |
| 维护 Skill 链接 | `/root/.codex/skills/meta-violation-material-cleanup` |

`config.example.json` 包含所需非密钥字段；`live_enabled` 初始为 `false`。
新安装时复制为运行目录 `config.json` 并设为 `0600`，核对引用路径可读。
不要覆盖已有生产配置。保持数据盘挂载；数据盘或共享账本不可用时停止新写入。
Python 要求 3.9 或以上。service 用 `/usr/bin/env python3`，明确 PATH 为
`/usr/local/bin:/usr/bin:/bin`；部署前核实此 PATH 实际解析到已验证的 CPU Python。

窗口是每轮截止时间向前 `lookback_days * 24` 小时，按 vio `created_at` 的半开区间统计。
用户 2026-10-24 要求当前这轮完整保留 30 天清单，完成后新扫描改为近 3 天。
生产配置 `lookback_days=3` 只影响后续新扫描；已冻结清单继续使用原窗口和证据。
字段支持整数 1–30，缺省 30 兼容旧配置。Creative 待办队列不按该窗口清除。
每轮按实际存在的各 `delete_status` 状态完整读取窗口内记录；索引为 `dc(delete_status,created_at)`，
按状态分开 keyset 读取。不能只按新增 ID 或 `updated_at` 增量，旧违规记录会更新。
所有定时 SQL 走 `63350` 只读端点和 `/usr/bin/mysql` FIFO Gate。

`graph_read_workers` 仅控制 Meta GET，支持整数 1–16，默认 4；不会并发删除或 SQL。
生产可在只读吞吐验证后提高该值。`status` 的 `graph_metrics` 保存 GET、重试、限流
及 usage header 数字统计；某类 header 的 observation 为 0 表示未取得该统计，不能视为零占用。
生产使用 `scanner=material_indexed`：违规素材 → `ads_source` 别名 → 发布表的
`ad_id` / `creative_id` → 指定 ID 的 Meta GET，每次至多 50 个 ID。
标量原始素材和 source ID 使用已有索引；CSV、JSON、空白编码沿这两个字段的索引
分别补查。相邻字符串范围覆盖所有非 NULL 值，利用已验证的 Index Condition Pushdown
先在二级索引中筛选，再读取匹配行的产品和完整字段，避免读取数千万条产品历史宽行。
每页最多 2,000 行，保留完整 keyset 分页；范围边界不是数量截断。
发布记录不受违规筛选窗口限制；完整读取每个目标 Ad 的当前关联和凭据，再核对 Meta 返回的
当前 Creative。SQL 会话在 Meta 读取前释放。常规扫描不再遍历全部账户库存。
`mysql_compress=true` 默认请求 MySQL 协议压缩，已在 FIFO 只读端点以完整行哈希验证等价；
不改变 SQL 门禁、端口或查询条件。广告关联按最多 1,900 个 Ad ID 查询，素材/凭据关系
仍按最多 1,000 个 ID 批量查询，
产品、授权和广告关系采用 keyset 分页，每页最多 2,000 行；共享 Creative 引用也合批读取。

执行先核验完整 manifest 的 SHA，再复用该轮完整窗口冻结的违规证据；不会为每条候选
重读历史 vio、历史违规 Ad 或重新计算阈值。扫描后的历史记录修改留到下一轮完整扫描。
每次 Creative 组预核和每条 Ad 临删核验，仍重新查询该 Ad 的全部当前关联记录，按当前
Creative 精确核对产品及直属根产品、素材、发布队列和凭据；不缓存这些当前关系，也不
重读整个产品目录。查询共用一条 FIFO 只读快照，在读取 Meta 当前审核、广告和父级状态前释放。
当前产品、素材、Creative 或凭据与冻结清单不一致即拒绝该对象。

`execution_order=ads_first` 执行三个阶段：先全部合格 Ad，再全部 Creative，最后处理
Creative 明确共享引用失败（3858800）的依赖。只有最后阶段才枚举相关账户的当前 Ad，
核对精确引用及当前审核状态，补删全部正常共享 Ad 后再统一重试 Creative。用户 2026-10-24
已授权删除此类共享 Ad，包括原素材规则之外的共享引用。当前违规或待审的共享 Ad
记录状态并留到下轮重新核实；不能根据上轮状态直接删除。

每条 Ad 删除前把 Creative 的来源、清单 hash 和冻结证据保存到本地 `cleanup_backlog`；
已删除 Ad 的共享账本 receipt 与实时 SQL 关系共同支持后续 Creative 清理。新一轮
即使该 Ad 已删或原违规窗口已过，仍加载此队列。只有确认 Creative 已删才移除。
后台预演与旧删除进程并行时，预演冻结之后仍会新增队列。交接必须先取得旧任务锁、
排空全部请求，再读取最新队列；使用 `handoff.derive_with_backlog` 生成新 run ID 的衔接清单，
逐条校验新增 Creative 的原清单 hash、完整候选依据和冻结证据。保留原预演文件、
`frozen_at` 和窗口，不能借交接延长一小时有效期。该操作只补 Creative 恢复项，不新增 Ad。
在停止旧进程之前以及导入新运行之后均核验队列零遗漏；已删回执及 unknown 防重记录沿用。
如果后台交接失败但旧服务和 PID 仍正常，告警须明确“交接失败、原删除仍运行”。
确认账号 `account_status=2` 后，Ad 无法操作时仍对准确归属的 Creative 尝试删除；
不假设账号被封就一定能删或一定不能删，保存 Meta 实际返回。权限错误本身不证明封号。
此修改不提高 DELETE 并发，也不改变 unknown 隔离和旧任务锁优先级。

## 常用命令

所有 CLI 参数中的 `--config` 都位于子命令之前。以下 `RUN_ID` 替换为真实保存的运行 ID。
命令不输出 env、Token、Feishu 密钥或完整密钥配置。

```bash
python3 /mnt/data-disk/meta-violation-material-cleanup/current/server-monitors/meta-violation-material-cleanup/run.py --config /mnt/data-disk/meta-violation-material-cleanup/config.json status
python3 /mnt/data-disk/meta-violation-material-cleanup/current/server-monitors/meta-violation-material-cleanup/run.py --config /mnt/data-disk/meta-violation-material-cleanup/config.json status --run-id RUN_ID
systemctl status meta-violation-material-cleanup.timer meta-violation-material-cleanup.service --no-pager
systemctl list-timers meta-violation-material-cleanup.timer --all --no-pager
journalctl -u meta-violation-material-cleanup.service -n 120 --no-pager
```

预演会保存本地审计材料，**不发送 Meta DELETE、不写业务源库**：

```bash
python3 /mnt/data-disk/meta-violation-material-cleanup/current/server-monitors/meta-violation-material-cleanup/run.py --config /mnt/data-disk/meta-violation-material-cleanup/config.json preview --run-id RUN_ID
```

真实执行需要生产配置已开启 live，且处于用户授权的执行范围。已有授权无需重复确认。
`--max-ads` 用于同一轮的受限验证；正式 timer 不设置此参数：

```bash
python3 /mnt/data-disk/meta-violation-material-cleanup/current/server-monitors/meta-violation-material-cleanup/run.py --config /mnt/data-disk/meta-violation-material-cleanup/config.json run --run-id RUN_ID --max-ads 1
python3 /mnt/data-disk/meta-violation-material-cleanup/current/server-monitors/meta-violation-material-cleanup/run.py --config /mnt/data-disk/meta-violation-material-cleanup/config.json run --run-id RUN_ID
```

同轮继续必须保留原始窗口、清单、成功 receipts 和 unknown fences。
只允许从 `previewed`、`partial`、`interrupted` 状态继续；`completed` 返回既有结果，
其他状态报 `existing_run_requires_recovery`。清单中的 `frozen_at` 是完整扫描结束并冻结
清单的时间；转 live 或再次继续时要求该时间距当前不超过 **3600 秒**，不能使用扫描
开始时间代替。未来时间、缺失时间或 hash 不一致均拒绝执行。

遇到 `manifest_expired_start_new_scan` 时，在原账本和 receipts/fences 完整保留的前提下
以新运行 ID 完整重新扫描、生成新的窗口和清单；不得编辑 `frozen_at`、hash 或复用旧清单
绕过期限。新扫描不会解除旧对象的 unknown fence。其他不可继续状态应先查实际状态，
不能更换 ID 绕开限制。

`--max-ads` 按同一 Creative 下的完整候选 Ad 组选择，不拆组；如果没有组能装入限制，
可以删除 0 条而返回 `partial`。继续时仅选择仍为 pending 的对象，不重做成功、failed
或 unknown 对象。
不得把 `--max-ads` 当成每轮永久截断，也不能把受限验证称为全量执行完成。

仅核对未知删除结果，或仅恢复完成播报：

```bash
python3 /mnt/data-disk/meta-violation-material-cleanup/current/server-monitors/meta-violation-material-cleanup/run.py --config /mnt/data-disk/meta-violation-material-cleanup/config.json reconcile --run-id RUN_ID
python3 /mnt/data-disk/meta-violation-material-cleanup/current/server-monitors/meta-violation-material-cleanup/run.py --config /mnt/data-disk/meta-violation-material-cleanup/config.json notify --run-id RUN_ID --phase end
```

`reconcile` 只做 Meta GET；`notify` 只恢复该轮通知，不重新扫描或触发删除。
通知请求结果不明确时先核对发送记录/回执；不得用重新 `run` 来补播报。

Meta multi-ID GET 遇到一个非法对象可能返回整批 `100/803`。新版会二分拆查这种明确
对象错误，不放大 Token、限流或网络失败。旧版已完成预演可在原 3600 秒期限内，用
`verify_preview_batches.py --config READ_ONLY_CONFIG --source-run-id SOURCE_RUN --run-id VERIFIED_RUN`
进行 GET-only 补核；可加 `--wait` 等待源预演结束。它只处理受影响的读取结果，并对
恢复可读且当前状态可能合格的 Ad 重新查询完整发布关系和凭据。派生清单保留原窗口、
原 `frozen_at` 和原 manifest hash，不修改原清单，也不续期。超过期限仍必须完整重扫。
补核后检查 `batch-verification.json`、剩余不可读对象及更新后的 `comparison.json`；
正式执行使用新的 verified run ID。该程序不打开删除账本、不发送通知、不修改定时器。

同一运行 ID 的第 1 次 live 使用通知 phase `start` / `end`；第 2 次使用
`start-2` / `end-2`，后续依此类推。`status --run-id` 返回 `execution_attempt`
和当前这次的通知 phase。`notify --phase end` **不会自动选择最近一次**；例如补第 2 次
完成播报必须指定 `notify --run-id RUN_ID --phase end-2`。已成功发送的 phase 直接返回
保存的回执；failed 可用原 UUID 和冻结正文重试；sending / unknown 拒绝自动重发。

完成播报超过单条上限时按实际外层 JSON 与内层 `content` 的 UTF-8 编码大小分片，
每片最多 **100,000 字节**，全部关联 Ad 信息保留。它仍算一次逻辑完成播报；父 phase
例如 `end-2` 保存完整正文，子 phase 为 `end-2-part-001`、`end-2-part-002` 等，每片
各自保存稳定 UUID、冻结正文和回执，只有全部子片 sent 才把父 phase 标为 sent。
补发仍使用父 phase；failed 父播报重试跳过已 sent 的子片，沿用原分片计划。
任一子片或父发送状态为 unknown/sending 时先核对回执，不自动重发，也不重新执行删除。
明确 HTTP 4xx 且有合法非零整数错误码的拒绝记 failed，只保存 code/http_status；
5xx、无明确拒绝码、超时和缺少 message_id 均不能当成确定未发送。

## 部署与验收

1. 在独立分支测试并提交/push GitHub，CPU fetch 精确 SHA 到
   `releases/<sha>`。保留旧 `current` 指向、当前配置和 systemd 文件备份。
   不在服务器运行副本上做无法回溯的补丁。
2. 先安装配置与 unit 文件，保持 timer 未启用、live 关闭。检查 Python、数据盘、
   `@@read_only=1`、FIFO Gate、Graph 读取、郜远接收人配置及共享账本可用性。
   不打印凭据。不要启动、停止或重启主 API/社媒发布服务。
3. 验证 service/timer 文法及北京时间调度，运行预演并保存清单/hash、政策证据、
   账户覆盖率、未知/跳过分类。相同素材的不同违规 ad 去重；阈值 3 不触发规则 2，
   阈值 4 才触发；暂停含父级暂停。无法确定的多素材归因必须排除。
4. 用单独的 live=false 配置设置 `comparison_baseline_run_id`，复用旧清单的冻结
   违规证据进行完整只读对照。检查 `comparison-retrieval.json` 的无法解释漏选为 0，
   再检查 `comparison.json` 的当前候选差异及耗时。对照配置和清单禁止转真实执行。
   随后去掉 comparison 配置完成按配置窗口的新预演，核对 hash/范围/错误分类。
   也可用 `comparison_reference_run_id` 一次完成两份结果：反查素材取新旧证据并集，
   用旧冻结阈值单独生成对照；真实执行清单只使用本轮新窗口证据。验收要求对照无
   无法解释的漏选，且产品范围相同；对照状态差异单独报告。正式配置移除 reference
   参数，后续定时无需持续对照旧清单。`comparison_baseline_run_id` 仍是禁止执行的
   纯旧窗口对照模式。
5. 按已授权范围开启 `live_enabled` 并执行新鲜预演的完整清单。检查全局 Ad→Creative
   →共享补删顺序、共享 claim、DELETE 接受与精确 DELETED 回读、飞书开始/结束回执。
   启用 timer，读回 `ActiveState/UnitFileState/NEXT` 和当前 release。
   `Persistent=true` 意味着补偿错过的调度可能立即触发，安装时必须考虑这一点。
   后续一次真实定时运行的审计记录才证明调度执行，NEXT 本身不是执行成功证据。
6. 将 Skill 目录链接到 `/root/.codex/skills/meta-violation-material-cleanup`，目标为
   `/mnt/data-disk/meta-violation-material-cleanup/current/server-monitors/meta-violation-material-cleanup/skills/meta-violation-material-cleanup`。
   已存在非本任务的同名文件/目录先核对，不能直接覆盖。

可用的部署校验命令：

```bash
systemd-analyze verify /etc/systemd/system/meta-violation-material-cleanup.service /etc/systemd/system/meta-violation-material-cleanup.timer
systemd-analyze calendar '*-*-* 09,21:00:00 Asia/Shanghai'
systemctl daemon-reload
systemctl enable --now meta-violation-material-cleanup.timer
```

## 结果口径与故障处理

- 扫描范围按实际成功分页的账户、广告和时间区间报告；不可读取账户、分页中断、
  产品/素材关系不明均单列。未发现目标不等于完成全范围扫描。
- `API accepted` 表示平台明确接受删除请求；`verified deleted` 表示对精确对象与
  账户回读到 `DELETED`。两种证据分别记录，不能仅用请求数当删除成功数。
- 当前待审核、处理中、已违规、已删除/归档、父级或自身状态不明/矛盾的广告跳过。
  历史违规但当前审核正常可按两条规则处理。
- 超时、断连、进程中断或不确定响应进入 unknown。暂停该对象的新 DELETE，保留
  共享 fence，先 `reconcile`。权限错误、`100/33` 或模糊“找不到”不证明已删除。
  不自动换 Token 重放。已经成功的对象不能因新一轮而再次写入。
- 通知失败与清理结果分开报告。仅通知失败时不重新执行清理；查看回执后使用
  `notify --phase end` 恢复。
- service 最长运行 48 小时、无自动重启，收到 SIGINT 后有 180 秒收尾。停止前后
  比较共享账本和 run 状态，不能把被中断工作改成全部成功。
- `status` 只查看状态，不运行 owner 恢复。恢复必须先取得本任务 `process.lock`，
  仅确认原进程 PID 与启动身份已终止后，标记本运行为 interrupted；未知存活状态保留
  fence。共享恢复须严格限定 `policy-cleanup-<RUN_ID>`、自动 actor 和属于该 job 的
  shared run，不能调用全局 `Store.recover_interrupted()` 影响其他任务。
  本地 `recovery_pending=true` 表示共享恢复尚未确认完成，跨进程重启继续保留并重查；
  只有成功核对并完成该 job 的共享 fence 后才能清除。不可单因本地已 interrupted
  就认定两个账本均已完成恢复。
- 服务重启不会自动续跑旧清单：unit 的 `run` 没有 `--run-id`，启动的是新扫描。
  需要继续旧清单时先查看其状态、通知及未知对象，核对 3600 秒有效期后使用上面的
  显式同轮命令。停止/回退不能通过恢复旧 SQLite 来撤销已发生的请求。
- 旧 10 点清理使用原有锁。此任务在北京时间 09:55–10:05 不开始新 DELETE；若正在
  组内预核，释放锁后等待重新获取并重核。已经完成的 Creative 不重发，余下 Ad 按
  当前依赖和状态继续。锁等待属于本轮耗时，不能把等待视为已完成。

## 回退

停止本 timer，若本 service 正运行则发送正常 stop 并等待收尾；不要停止其他服务。
保存当前 run 状态后，将 `current` 切回事先验证的旧 release，按备份恢复本任务配置/
unit，并执行 `daemon-reload`。先保持 live 关闭，核对健康与账本后再恢复已授权调度。

**回退只回代码、配置和 unit；绝不覆盖共享 SQLite、运行证据、发送回执或成功 receipts。**
删除已发生的 Meta 对象不能通过代码回退恢复。保留 unknown fence，使用 GET-only 核对。
如果旧版本不兼容当前账本，保持停用并修复兼容性，不能降级数据库。

## 原 v1 首次上线的持久验收任务（历史流程）

下面的 `bootstrap.py` 仅适配原 v1 策略，不用于 `20261024-v2`。保留原失败状态和
通知记录，不删除它们来重放旧验收。v2 使用上方的只读对照、当前窗口预演和正式服务
启动流程。首次启动可以用 `/run/systemd/system/...service.d/` 临时指定新鲜预演 run ID，
服务确认启动后移除该临时覆盖并 daemon-reload；后续定时仍启动新扫描。不得永久绑定旧清单。

`bootstrap.py` 只编排已运行的完整预演、少量真实验证、继续执行和 timer 启用；实际删除
始终经过同一 `run.py`、开始消息回执、共享对象锁及未知结果隔离。它不新增公共接口。
必须使用 GitHub 已提交的精确 release 路径启动，传入该 release 的完整 commit SHA：

```bash
python3 /mnt/data-disk/meta-violation-material-cleanup/releases/RELEASE/server-monitors/meta-violation-material-cleanup/bootstrap.py --config /mnt/data-disk/meta-violation-material-cleanup/config.json --run-id PREVIEW_RUN_ID --release-sha FULL_COMMIT_SHA
```

生产由独立一次性 systemd unit 托管，`Restart=no`，SIGINT 正常停止；退出聊天不会结束它。
开始时要求 live 关闭、timer disabled/inactive。等待期间不发送 DELETE；只有完整预演
结束后，核对清单 SHA、30 天窗口、全部账户的成功/失败覆盖、候选规则及产品范围，才
开启 live。若预演耗时达到 10 小时，则停止上线并报告容量问题，不能启用已无足够执行
时间的 11 小时定时服务。该门禁不能被当作全量耗时已达标的证明。

少量验证按完整 Creative 组选择，最多 3 条 Ad；对有对象的验证，要求 API 接受且每个
Creative/Ad 都精确回读到 DELETED，并核对真实首尾飞书回执。零候选则核验真实零结果
及消息回执，并明确记录未验证实际删除。只有验证通过，才继续处理冻结清单中的待处理
对象。首次存量清理可能持续较长时间，正式 timer 在继续执行完成并通过账本验收后启用。
明确失败或持久隔离的未知对象保留为部分完成，不恢复或重发已发生的请求。

验收状态在数据盘 `bootstrap-PREVIEW_RUN_ID.json`，完整预演验收摘要在
`rehearsal-acceptance-PREVIEW_RUN_ID.json`。失败进入 `stopped_for_review`，关闭本任务
live/timer，保留所有证据，并通过独立 `PREVIEW_RUN_ID-bootstrap` 通知账本给郜远发送
上线异常播报；该通知不会占用正式运行的 start/end。通知不确定时不自动重发。
重启 bootstrap 不会自动重放已启动的流程；先查看状态、通知和对象账本，再按已授权
范围人工核对、使用正式 CLI 继续或重新完整预演。不得删除状态文件绕过这一限制。

停用首次验收时，应同时正常停止对应的 bootstrap unit 和尚在运行的 preview unit；
只停 timer 不会停止首次验收。若 bootstrap 正在等待预演，停止后要保留预演及通知证据。
回退代码前先停止这两个本任务 unit，保留共享账本和本地 SQLite，不能让旧验收任务
在回退后继续自动启用 timer。


## Bounded concurrent execution (2026-10-24)

The operator authorized concurrent deletion after the first live run measured
roughly 4 seconds for per-Ad SQL/preflight and 2 seconds for mutation/readback.
Production: execution_workers=32, execution_per_account=2, execution_batch_size=64,
execution_context_ttl_seconds=30, execution_max_rps=24. Setting workers to 1 keeps
the serial fallback. Workers do network I/O only. SQL and both durable ledgers
stay on the owner thread. Each wave freshly reads joined publication, product,
credential and source relations through the existing FIFO read-only endpoint.
The exact final Meta GET checks the full frozen policy and also acts as the
identity guard, removing a duplicate preflight GET.

The governor starts at 4 and ramps by 2 after 20 seconds of observed low usage.
GET and DELETE share its global and per-account concurrency/rate budget.
High usage and explicit rate errors reduce concurrency and apply cooldown.
A phase drains all workers before the next phase starts. Unknown DELETEs never
replay. Only provably unsent requests due to stale context or legacy priority
may refresh SQL and retry at most twice. Shared remediation retains its global
Ad-before-Creative-retry barrier and uses the same mutation governor.

Rollout: gracefully stop the service, inspect claims and receipts, deploy an
immutable GitHub release, and continue the same run within its original one-hour
conversion deadline. If expired, keep the active job running while a separate
read-only staging preview refreshes the complete window. At cutover, stop and
drain the old owner, verify the fresh manifest and complete backlog coverage,
and import only the new preview into the production runtime under its lock.
Never renew a manifest timestamp/hash. Preserve unknown
fences, receipts and backlog. Observe accepted+verified throughput, usage and
rate responses before quoting an ETA. The operator extended the service runtime limit to 48 hours on 2026-10-24,
so the Creative and shared-reference phases have time to drain. The timer does
not launch a second process while this service is active; retain both locks.

Execution waves start at at most 4 times the current request concurrency and
expand to the configured batch maximum (64) as observed headroom permits.
Request scheduling waits for the exact next rate-budget deadline, rather than
using a coarse polling interval that unintentionally caps throughput.
# 2026-10-25: dependency 1487235 and live reconciliation

Both definite dependency rejections `1487235` and `3858800` enter shared-reference
remediation. A saved rejection skips the redundant first Creative DELETE. All
shared Ads finish before any Creative retry. Global receipts suppress every
subsequent DELETE; unknown/in-progress fences prevent dependency cleanup.

`repair_background.py` is a one-shot CPU operation for `scale32-20261024-v2`.
It leaves the live process running and uses GET-only batches (50 objects, serial,
at most 2 request starts/second, with Meta usage/cooldown feedback). It snapshots
all unknown Ads/Creatives and the 9,514 old pending Ads absent from the new job.
Only exact ID/account/status=DELETED proof atomically updates the shared ledger
and global receipt. In-progress or changed rows are untouched. Source credentials
are read through the FIFO SQL gate, and that session closes before Graph calls.

The operation's durable state and target ledger live under
`/mnt/data-disk/codex/tasks/meta-cleanup-indexed-20261024/repair1487235/`.
After natural completion and service exit it starts the normal service once,
using the configured 3-day window plus the complete durable recovery queue from
the 30-day run. It never modifies an expired manifest or its frozen timestamp.
An uncertain service start is not replayed. Private start, first-pass, completion,
and exception notices use the existing fixed recipient and durable message UUIDs.
The original run's in-memory counters remain historical; the repair target ledger
and shared object ledger are the authoritative reconciled counts.
