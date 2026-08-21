# 测试报告

## 测试结论

通过。最新回归为FB专项128项与X/TT合并基线66项，另已完成CPU/GPU生产部署、30日指标回填和GPU→COS canary。2026-08-21经用户单独授权，仅对一个冻结的随机可发布Page执行了一条真实帖子canary并确认`published`；随后release `af1c3b1`完成日预制部署，首条自动任务已ready，并于2026-08-21 17:14:48 CST开启持久化live。首个自然到点批次尚未发生，其最终Graph对账仍待自然观察。

2026-08-20 `{{desc}}/{{url}}` 扩展通过：92项FB专项 + 93项GPU/TT短链/X-TT合并基线，共185项本地测试全部通过；生产release再跑92项FB测试通过。生产只读预检确认 MySQL `@@read_only=1`，描述聚合使用 `content_id` 索引；真实 schema 的同身份描述可确定性收敛为1值。closed-gate部署和线上验收已完成。

2026-08-21 模板启用查询兼容性修复通过：本地与生产新release各92项FB专项测试通过；生产只读端口复现并关闭 MySQL 5.7 错误3065，修复后的旧队列冲突查询返回0条冲突。Page池62为13/8个总Page/可发布Page，指标active pointer READY，模板、运行和Graph状态均未被验证过程修改。真实发布总开关仍关闭。

2026-08-21 单Page真实canary通过：首次复用冻结随机意图时，完整素材PRIMARY扫描达到600秒截止并以`fb_auto_catalog_scan_timeout`停止，未创建run/task且GPU/Meta调用均为0。release `9f1f5b268766e1c25fbe3081bd0505978510b78e`新增按指标剧集走`(data_source,data_source_id)`索引的精确候选预筛；只有过滤后的优先集合填满5000条候选时才跳过全表扫描，否则回退原完整keyset扫描。生产只读真实配置基准从超时降至142.266秒并返回5000条候选。本地95项FB专项与66项X/TT合并基线全部通过，生产release再跑95项FB测试通过。

2026-08-21 北京自然日提前预制改造本地与独立复审通过：新增prebuild/live双门禁、当天冷启动只建明日、跨午夜持续模板补今天剩余、次日5时隙一次冻结、`prepared_at_utc`、10分钟自动迟到截止、enabled/current-version门禁、manual停用永久取消、running不可逆Meta提交边界、due租约ABA防护和legacy ready fail-closed迁移。FB专项128/128、X/TT合并基线66/66、py_compile与diff-check均通过；部署前审计时生产仍为原release、live=false、0 due、0 running/ready，只有既存单Pagecanary事实。

2026-08-21 prebuild-only生产灰度先完成代码、计划与首条GPU预制验证：当前release=`af1c3b1`，代码切换前备份目录为`/mnt/data-disk/fb-auto-post-deploy/backups/20260821T164759+0800-pre-af1c3b1`。明日计划正好5个时隙，Page计划快照为13个Page/8个可发布/5个缺Token；FB专项128/128、X/TT合并基线66/66通过。首条自动任务task 4/run 2于17:04:41 CST开始、17:14:03 CST ready，成片58,144,303 bytes、SHA=`cf2b71128cefd8c62d209fa23015195e73e532aa9487e9bbe02d96033075da46`、profile=`tt-post-random-overlay-h264-720x1280-v3`，公开HEAD 200且元数据一致。

2026-08-21 17:14:48 CST在running=0/preparing=0/ready=1的切换门禁下开启live，切换前两库备份为`/mnt/data-disk/fb-auto-post-deploy/backups/20260821T171448+0800-pre-live-af1c3b1`。切换后health的prebuild/live均为true，服务`NRestarts=0`；4次execute均为no_pending、reconcile为no_submitted、attempt仍2、ledger仍1、early running=0，未来任务没有提前发布。第二条自动任务task 6于17:14:51 CST开始、17:16:46 CST ready，成片20,205,203 bytes；第三条自动任务已进入preparing，attempt/ledger仍无增量。首个自然到点批次最终对账保留为未来观察项。

同一冻结Page `967347116442420`（`कहानी के दृश्य`）随后仅创建run 1/task 1；素材`6281282`、content `XtTulNgWI1`经GPU生成475.766667秒、285,917,510 bytes的独立H.264成片。Graph首次授权被明确拒绝为190，第二个同Page可用授权返回对象`1051031017645759`；首次自然回查确认`video_status=ready`、`publish_status=published`，永久链接`https://www.facebook.com/reel/1051031017645759/`匿名HEAD为200。最终run=`completed`、task/ledger=`published`、unknown=0，且总量保持1 run、1 task、1 ledger、2 attempt。

## 测试范围

FB validation/repository/store/publisher/app contract、V2 metric/due queue/GPU prepare-only/容量/部署契约，以及上一轮 X accounts、TT auto、X auto 主 API 合并基线。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| FB V2 专项单元/契约 | 128 | 128 | 0 | 0 |
| X/TT 合并基线 | 66 | 66 | 0 | 0 |
| 当前累计证据 | 194 | 194 | 0 | 0 |

## 缺陷情况

BUG-001 SQLite连接泄漏、BUG-002 metric SQL `ONLY_FULL_GROUP_BY` 1055、BUG-003 GPU系统Python版本不兼容、BUG-004 oneshot忽略`RuntimeMaxSec`、BUG-005 MySQL/Python跨content排序语义不一致、BUG-006 COS SDK自定义元数据键缺少协议前缀、BUG-007 625万行素材PRIMARY全表扫描超时、BUG-008 running期间升版竞态、BUG-009 disabled manual延迟恢复、BUG-010 planner租约ABA、BUG-011 legacy ready永久backlog均已修复并回归。无未关闭本地缺陷。

## 验证证据

- `python -m unittest discover -s scripts -p "test_fb_auto_*.py"`：128/128 PASS；其中GPU prepare-only契约包含在当前FB专项范围。
- `scripts.test_x_accounts_app_contract scripts.test_tt_auto_publish_app_contract scripts.test_x_auto_publish_app_contract scripts.test_tt_posts_app_contract`：66/66 PASS。
- `python -m py_compile ... app.py`：PASS。
- `node --check static/quick-nav.js`、navigation JSON 解析：PASS。
- 最终 inline JS、diff 和敏感扫描见本次交付命令输出。
- 生产页面：模板页与发布记录页均HTTP 200；已登录DOM显示视频制作模板=随机排重模板、Page池/素材条件/固定或每日随机频率；管理API匿名访问401。
- 生产闭锁：scheduler/plan/prepare/runner/reconcile自然timer均返回 `live_gate_closed`，六张发布操作表始终为0；sidecar/main API/GPU/tunnel `NRestarts=0`。
- 指标：2026-07-19至2026-08-17共30个active READY pointer、669,299日明细行、0 building，两个SQLite `quick_check=ok`。
- GPU/COS：首次17.218秒、二次0.002秒复用；公开HEAD 200、`video/mp4`、3,306,811 bytes，SHA/profile元数据一致；成功job仅manifest，GPU数据盘101G可用。
- 单Page真实canary：生产成片HEAD 200、`video/mp4`、285,917,510 bytes，COS SHA/profile与SQLite/GPU manifest一致；短链`https://gy.g2flow.com/s2l/fb/1.html`返回200及`no-store`，wrapper目标为指定`/ads/0/2049/view`且包含`af_channel=AIpost`；Graph永久链接返回200。
- 生产状态：operational/metric SQLite `quick_check=ok`；sidecar PID `1062207`、`NRestarts=0`、部署后warning日志0；七个timer均active但持久化health仍`live_enabled=false`，因此不会继续创建或发布其他任务。
- 日预制与live：release `af1c3b1`；代码备份`20260821T164759+0800-pre-af1c3b1`；明日5个时隙；计划13个Page中8个可发布、5个缺Token；FB 128/128与X/TT 66/66回归通过。
- 首条自动GPU ready：task 4/run 2，17:04:41→17:14:03 CST，58,144,303 bytes，SHA=`cf2b71128cefd8c62d209fa23015195e73e532aa9487e9bbe02d96033075da46`，profile=`tt-post-random-overlay-h264-720x1280-v3`，公开HEAD 200且元数据一致。
- live切换：17:14:48 CST，切换前running=0/preparing=0/ready=1；两库备份`20260821T171448+0800-pre-live-af1c3b1`；health的prebuild/live均为true，`NRestarts=0`。开启后4次execute no_pending、reconcile no_submitted、attempt仍2、ledger仍1、early running=0。task 6于17:14:51→17:16:46 CST完成第二条自动ready，20,205,203 bytes；第三条自动任务已preparing，attempt/ledger仍无增量。
- `[待观察]` 首个自然到点批次的task/attempt/ledger/Graph最终对账或回滚结果；尚未发生，不能提前填写。

## 遗留风险

- 修复前 SQL 已由生产只读 EXPLAIN 复现 `ONLY_FULL_GROUP_BY` 1055；候选修复后生产只读 EXPLAIN、单日23,765行和最近30日完整刷新均通过。content ID 使用binary canonical排序，素材ID使用ASCII正整数长度+字符串数值序。
- Graph v22.0 已完成一次明确授权的单Page真实帖子canary；该证据只证明当前Page、当前素材及当前Token轮换路径可用，不代表13个Page或自动批量调度已经放开。
- 首发不支持跨产品，服务端固定 Dramawave `app_id=1479/data_source=6/metric_product=Dramawave/platform=0`，未知映射 fail closed。
- GPU已有单任务基准，但尚无20任务/同槽连续吞吐基准；默认 `20 jobs/slot` 不得上调。
- 实际资产manifest、COS public-read HTTPS回源与NVENC已通过；除已审批的单Page单帖canary外，其余真实发帖、自动调度和扩量仍需另行明确审批。
- GPU processor和prepare unit统一为串行1任务；当前101G可用满足40G静态门禁，但持续磁盘告警与live前水位复核仍需完成。

## 发布建议

单Page真实canary、首条自动GPU ready及live切换均已完成，当前health显示prebuild/live均为true；明日5个自然时隙的计划已存在，且开启后未提前发布。是否能按计划完成每天5次真实发布，仍需以首个自然到点批次的ledger/Graph最终对账作为生产闭环证据，不得把计划存在、ready或timer active当作发布成功。

2026-08-20 `{{desc}}/{{url}}` 扩展部署本身保持closed-gate，未创建生产模板、任务、wrapper或帖子；2026-08-21另经单帖授权的canary才创建唯一wrapper与唯一真实帖子。

2026-08-20 线上证据：release `1b9fe57...`、备份 `20260820T183309+0800-pre-1b9fe57a`；sidecar/Nginx `NRestarts=0`，七个timers active，六张业务表和wrapper均为0，X/TT/TT-auto既有短链样本200，FB短链缺失/非法/POST分别404/404/403，页面列表/创建分离及两宏可见。

2026-08-21 单Pagecanary线上证据：release `9f1f5b268766e1c25fbe3081bd0505978510b78e`；部署前备份`20260821T112904+0800-pre-9f1f5b2`，对账前submitted事实备份`20260821T115928+0800-pre-task1-reconcile`。最终对象`1051031017645759`为`published`，当时persistent gate为0。

2026-08-21 当前线上证据：release `af1c3b1`；代码切换前备份`20260821T164759+0800-pre-af1c3b1`；明日5槽，Page计划13/可发布8/缺Token 5。task 4/run 2已ready；17:14:48 CST在备份`20260821T171448+0800-pre-live-af1c3b1`后开启persistent live，health为`prebuild=1/live=1`，`NRestarts=0`，无提前execute/reconcile事实。`[待观察]` 首个自然到点批次最终对账。
