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

窗口是每轮截止时间向前 `30 * 24` 小时，按 vio `created_at` 的半开区间统计。
每轮按实际存在的各 `delete_status` 状态完整读取窗口内记录；索引为 `dc(delete_status,created_at)`，
按状态分开 keyset 读取。不能只按新增 ID 或 `updated_at` 增量，旧违规记录会更新。
所有定时 SQL 走 `63350` 只读端点和 `/usr/bin/mysql` FIFO Gate。

`graph_read_workers` 仅控制 Meta GET，支持整数 1–16，默认 4；不会并发删除或 SQL。
生产可在只读吞吐验证后提高该值。`status` 的 `graph_metrics` 保存 GET、重试、限流
及 usage header 数字统计；某类 header 的 observation 为 0 表示未取得该统计，不能视为零占用。
每批账户的素材与凭据查询共用一个 SQL 快照，完成分页后再持有 SQL 会话，避免等待 Meta 时占用 FIFO。
最多聚合 32 个完整账户，目标不超过 20,000 条库存广告；超过此目标的单个大账户独占
一批并完整处理，不能截断。查询结束后每个账户仍分别提交库存与扫描结果，不能把
合批查询视为合并账户的完成状态。
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

Creative 组全部候选预核完成后，以最后通过者作为删除依据。如果该候选之后还有其他
检查，临删前单独重核该候选；若已变化则移除，并重新核验前一通过者，直到取得当前
合格依据或跳过整组。等待旧任务释放锁后仍重新预核。每条 Ad 删除前再次核验，不复用
Creative 删除前的当前状态。此优化不提高 DELETE 并发，也不改变 unknown 隔离。

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
4. 按已授权范围开启 `live_enabled`，进行同轮受限 live 验证，再按既定范围继续。
   检查 Creative→Ad 顺序、共享对象 claim、删除接受响应与最终状态回读、开始/结束
   飞书回执。共享 Creative 按用户要求删除，不能扩大到非规则 Ad。
5. 确认完成后启用 timer，读回 `ActiveState/UnitFileState/NEXT` 和当前 release。
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
- service 最长运行 11 小时、无自动重启，收到 SIGINT 后有 180 秒收尾。停止前后
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
