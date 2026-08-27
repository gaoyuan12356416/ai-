# 测试报告

## 最终放行：PASS（2026-08-27 17:50）

978746f最终静态4副本/Git/public HTTP200哈希一致，17:45正式live/sync开启；17:49三个进程有效环境/服务/20历史done/唯一canary次数和新备份5成员+SQLite逐SHA复核通过。独立QA核对正式激活报告无新阻断。真实浏览器观察到立即显示加载提示且频道/提交禁用，加载后可选Shahrul且评论输入可编辑，随后关闭、未POST公开发布。BUG-028/029/030均完成实机闭环；完整证据与边界见[上线验收](release-acceptance-20260827.md)。以下各批测试只归属其明确候选，不累加、不改绑最终文档提交。

## 弹窗加载反馈增量独立QA：PASS（BUG-030）

两份同源HTML与新增Node测试共3文件冻结。独立一次新专项38/38+既有列表16/16通过；另12场景覆盖A→B→A、关闭/重开/等待旧请求、旧POST成功或失败到达已ready的新弹窗、拒绝重复确认、失败后同产品fresh重读，均PASS。Node语法、两页4段inline脚本、diff/新增文件尾空白及3文件前后hash一致通过。外部I/O为0、不是浏览器/生产测试；app/features/DB/RPC/CLI/deploy源码相对ee6不变。下一步精确GitHub静态部署与线上加载态核验，不重跑272/119整套、不合并累加历史批次。

## 真实集成验收：指定频道单次发布闭环 PASS（17:31）

ee6e00c CPU实机读取/上线与任务列表三入口通过，原样式/四项未选/移除下拉/CPU模板自动手动目录已在郜远会话核验；短链HTTP200、wrapper哈希及参数一致，列表再生成复用id1。指定Shahrul Ikmal发布任务1已完成：视频HGgjhhRXS-I、评论Ugwktiv9_nnXb1TN_c54AaABAg，attempt各1、unknown=false、视频/评论published、sync=synced。

17:26:50独立于发布执行的YouTube新鲜读回确认unlisted/processed/succeeded、标题/描述/评论文本/作者匹配、仅一条comment thread。ads_ai三表各1、完整载荷与对应outbox一致、3outbox各attempt1，原账号Token/client指纹前后不变。17:28:47同operation/task完成后重放成功，17:31:02验证4SQLite表和3MySQL新表计数及全行hash完全不变。此批是真实集成证据，不计入离线用例数量；完整报告见[上线验收](release-acceptance-20260827.md)。正式放行及BUG-030加载态增量另记，不提前宣称完成。

## 实机兼容修正定向 QA：PASS（BUG-028/029）

冻结的 HEX 读取与列表入口增量已独立复核，无新增 P0/P1。一次 `encoding + upgrade + canary` 定向 Python **119/119 PASS，6.717 秒**；两页 Node 行渲染 **16 checks PASS**。另独立纯内存读取/拒绝对抗 **6/6 PASS，0.222 秒**（包含实际 app.run_mysql AST 与 fake batch），Node **10 场景/20 次 handler 调用 PASS**；6 类外部 I/O 拦截均 0，非浏览器、非生产验收。3 Python compile/3.9 AST（实际 Python3.14.3）、Node --check/两页共4段 inline JS、diff 与新增文件尾空白、6 文件前后冻结 SHA 均通过。

原库仅 SELECT，两 JSON 列 HEX 一次严格还原；坏 HEX、坏 UTF-8、非对象 JSON、字面反斜杠 scope 仍拒绝，未放宽身份/scope。原 app/core/RPC/canary/DDL 不变；下方 272 只属于 59f95e6 基线，不重跑、不与119相加。代码可提交后进入 CPU 真实读取与同一 operation prepare；真实上传/评论仍未完成。

## 本次现有账号 v3 冻结 QA：PASS（2026-08-27 16:45）

独立 QA 唯一一次完整 10 suite **272/272 PASS，17.778 秒**（48+25+23+60+4+33+22+16+30+11）。另 15 项内存对抗：14 项首轮通过，1 项因 QA 夹具误选 DELETE 而非重复 SELECT，纠正夹具后仅定向复测通过；未修改源码、不重跑整套。35 文件 compile 与 Python3.9 AST 兼容通过，实际执行解释器 Python3.14.3。7 个冻结源码/配置/测试文件前后 SHA256 一致，diff-check PASS；DDL SHA256 08efc2e9d7e7bb52eb9bf041e9133acb214ca6dc8b8c7d86cb73d6d80ee8be38 未变。无新 P0/P1。108 专项和旧 262/23 均不与本次相加。

覆盖共享账号必要能力、TRIGGER 可见性、无 trigger/FK、精确身份/目标/五字段凭据、固定新三表 SQL、原表与任意 SQL 拒绝、旧 v2/虚假 DBleastpriv 健康拒绝且零 OAuth/上传/评论/claim。QA 全程零真实网络/数据库调用。代码放行不代表线上发布完成；下一步必须以真实服务用户验证 v3、CPU 切换及单次 Shahrul Ikmal unlisted 验收。

## 最新覆盖范围：现有账号 v3（2026-08-27 16:35）

按用户新决定与 [现行合同](ads-ai-new-tables-20260827.md)，不再创建专用数据库账号。CPU 使用现有 ads_aius 与已有频道授权，应用 SQL 仅限 ads_ai 新三表；原 MySQL 表只读。健康合同为 drama-youtube-writer-preflight-v3，shared-existing-account / application-table-allowlist / db_least_privilege=false；仅核验必要能力，不宣称全量 grant 审计。每次写前验证 TRIGGER 可见性和无 trigger/FK，旧健康合同拒绝。既有 DDL/v2 payload 与 UI 合同不变；下文专用账号/旧 v2 health 是历史。本轮专项 108/108，独立唯一完整回归及实机发布验收另记，不叠加历史批次。

## 历史实机结论：新表已创建，当时账号门禁未通过（16:06；已退休）

GitHub 精确候选 `6e29ba8c07c75dea98caff4d1d2b4fba0ac9df1f` 在 CPU clean checkout 完成：生产只读发现 PASS、全新隔离 MySQL 5.7.44 九项建表/载荷/幂等/隔离演练 PASS、生产仅 CREATE 三张 ads_ai 专用表 PASS。apply 后管理员验证无 trigger/FK；随后 63350 每表回读 0 行。原表未写入、未复制数据。报告及 SHA 见 [新表合同与证据](ads-ai-new-tables-20260827.md)。

独立最小权限 writer 未能创建：生产 ads_aius 缺少 CREATE USER，16:04:58 精确 GRANT 返回 1410；隔离同权限账户也拒绝。该问题是部署权限前提缺漏，不是表级写入失败，不能用广权限业务账号替代或将代码 QA 冒充线上发布通过。CPU 原应用/服务/20 个已完成任务不变，SQLite quick_check=ok；新 ffprobe 版本和 SHA 实机 PASS。真实 YouTube refresh/upload/comment 均为0，UI/18788尚未切流。后续须取得合法管理员安全配置，继续账号/RPC、CPU 切换和唯一频道验收，不重跑未变代码的完整回归。

## 最新增量：MySQL 5.7 库名授权兼容（BUG-026）

f3d754e2d4d0912d0cb7bf63b98d01c5f6f554bb 已经 GitHub push/readback，CPU 精确 clean checkout 的生产 dry-run 通过，三表均不存在。隔离 MySQL 5.7.44 授权显示为精确 `ads\_ai`，bootstrap 在身份预检处停止，0 张表、0 DDL；没有改动生产表。

两文件小修新增该精确显示形式，独立 23/23 定向测试及另 6/6 接受/拒绝内存对抗 PASS，网络/真实数据库调用均为 0；compile、Python 3.9 AST、diff-check 和测试前后 SHA 一致性通过。DDL、runtime parser、其余 16 源码/配置未变，原 262/262 证据保留为基线，不相加、不重跑、不改绑。新 GitHub 候选需重新运行生产只读发现和隔离 fresh-rehearsal。见 [BUG-026](bugs/BUG-026.md)。

## 最新增量：ads_ai 新表（代码冻结 QA PASS）

用户最新确认原表只读，改为 ads_ai 新建专用三表。独立 SA/代码 QA 无剩余 P0/P1，指定10模块一次合并 **262/262 PASS，14.628秒**（含 X30/TT11 契约）。另 **15/15** 独立内存对抗 PASS，0.017秒，网络/真实数据库 tripwire 均0；35文件 compile/3.9 AST PASS，本机执行器是 Python3.14.3，不冒称实际3.9运行。18个候选源码/配置测试前后 SHA 一致，差异检查无空白错误。

BUG-023/024/025 已修复并独立复核。实现专项39、root专项（含1项Windows测试连接未关闭后定向修复）、旧迁移4项均不与262相加。可进入 GitHub 精确候选和 SSH fresh-rehearsal 门禁；生产新表/最小权限writer/CPU切流/真实YouTube结果另行验证。下方204/188/166为旧候选历史，不冒充本轮新表验收。现行 [新表合同](ads-ai-new-tables-20260827.md)。

## 历史结论：CPU 查询边界（2026-08-27 14:47，北京时间）

CPU 新候选 `40042f9692fbec58caa5abbf41af35e9aefb54bc` 已 GitHub push/readback；模板目录只读 CPU 原始 manifest，不请求 GPU 或媒体素材包。独立七套唯一一次合并 **204/204 PASS，13.639 秒**；另外 15 项纯内存对抗 PASS，不叠加 204。3 个 Python 文件 compile/3.9 AST、4 文件冻结 SHA、diff-check PASS，无新增 P0/P1。

最后的 13 份文档一致性复核 PASS；修改文档后仅对受影响的部署/迁移文本合同定向复测，1/1 PASS、0.034 秒，不叠加 204 或伪称第二次整套。代码冻结未变化。

CPU 数据盘已安装 7921-byte、固定 SHA 的只读 manifest；GitHub 拉取精确候选到独立干净 checkout 后，CPU Python 3.9.6 使用真实文件执行 app 原函数（AST 提取），315 组合、auto/manual 冻结、坏配置/坏指纹 503 均 PASS；socket/HTTP/SQLite/媒体包 tripwire 为 0。不启动整个 app，不替代生产 HTTP 测试。生产 API/job worker PID、app.py SHA 不变，未改 env 或重启；具体路径、blob/recipe SHA 见 [职责与验证记录](cpu-gpu-boundary-20260827.md)。

HK 本次未更新/重启/重制，仍为 e1f5a1d，沿用下文真实媒体证据。旧三表演练仍只绑定 c719beb，不改绑新 CPU 候选。CPU 正式上线、HK 正式前缀激活与指定频道实际发布仍待生产数据库合法写权限及后续门禁，真实 YouTube 上传/评论仍为 0，整体 production release HOLD。当前状态见 [部署状态](deployment-status-20260827.md)。

已知非阻断风险：现有共享媒体 URL 下载器并非来源/重定向强白名单，正常业务链路无 GPU 业务查询不等于网络级隔离；本次不扩大修改 X/FB/TT 共享下载器。相关评审见 [SA 代码评审](sa-code-review.md)。实现者 16 项专项、旧 188/166 等均已包含于本轮 204 范围，不叠加；此次没有重跑历史浏览器或真实 YouTube 用例。

## 历史结论（2026-08-27 12:38，北京时间）

HK 隔离 release `e1f5a1d04cfb510df9c2444ac592adec2827508b` 的真实自动/手动合成、封面回调、下载解码、即时重复提交及服务重启后重放均通过；报告与 8 帧经独立只读 QA 复核 PASS。CPU 候选/三表演练仍绑定 `c719bebf72be900ec3853858dc53b36b83beffd2`，CPU 正式应用尚未发布/切流。生产数据库合法授权未具备，YouTube 实际上传/评论仍为 0，整体 production release HOLD。见 [部署状态](deployment-status-20260827.md)。

## HK 缓存增量独立复验（2026-08-27）

在 CPU 候选 c719beb 之后新增的 HK 缓存修复已独立 QA 通过，并由 GitHub 精确 SHA e1f5a1d 部署到 HK 隔离 release。v3 fresh 报告耗时 79.44 秒；实际重启 worker/tunnel 后，replay-only unit 运行 1.710 秒、Result=success。代码回归、主流程 SSH 实测和独立报告/8 帧复核分别取证，不将一种证据冒充另一种。

- 六套合并共 188 项，首次 187 PASS、1 项因迁移文档缺少实际错误字符串失败；补回说明后该项定向复测 PASS。没有重复整套，不称“一次全绿”。此统计包含下文 166 基础用例，不相加。
- 27 文件语法及 Python 3.9 AST、diff 检查通过；5 个缓存增量冻结代码文件测试前后 SHA 一致。
- 新版本清单校验失败、HEAD 异常和配方冲突不回退重制；独立发现的缺失 profile P2 已修复，缺失/错误 profile 在 HEAD 之前拒绝。见 BUG-020、BUG-021。

## CPU 候选 c719beb 基线与正式发布边界

代码候选 `c719bebf72be900ec3853858dc53b36b83beffd2` 已完成独立 QA，GitHub push/readback 已由发布主代理确认。代码 QA PASS，整体 production release 仍为 HOLD；不能把代码缺陷修复、隔离环境准备或表级演练等同于正式发布完成。

用户当前授权：所有支持与服务器操作仅通过 SSH；环境门禁通过后继续部署，并只在 **Shahrul Ikmal** 执行一次内部 `unlisted` 视频测试及一条评论。禁止进入/操作腾讯云管理后台，不允许 public 测试，不修改现有 X/`ads_video_producer` 业务。本轮文档更新没有服务器操作。

频道冻结为 app `1479` / local channel `263` / account `255` / YouTube channel `UCHJ1jFaYuW8g5EM7hM5pPpg`；唯一 operation 为 `drama-hk-deploy-unlisted-20260827-shahrul-263`。正式 HTTP/UI 仍固定 public，不能传入 canary 参数绕过正式开关；内部 CLI 的 live/sync 开关均保持 `0`。

保留附件已确认的上线风险：正式页面固定 public 与附件所引 YouTube 最低功能要求中的隐私选择要求存在合规风险。测试通过不能消除该风险；如平台审核要求整改，应在正式启用前由业务确认三态选择，不能擅自把测试视频改成 public 或将其隐私改成 private 来绕过评论验收。

### c719beb 阶段合并回归统计

| 验证 | 本轮结果 | 证据边界 |
| --- | --- | --- |
| 五个 Python suite 一次合并执行 | 166/166 PASS，12.151 秒 | 独立 QA；c719beb 阶段计数，已包含在上文 188 项内，不相加 |
| 25 文件语法及 Python 3.9 AST | PASS | 语法兼容检查，不替代 CPU/HK 运行验收 |
| 冻结范围与 diff | 12 个冻结文件测试前后 SHA 一致；`git diff --check` PASS | 文档后续更新不改变已测代码 |
| 独立媒体对抗 | 另 5 项 PASS | 内存 mock，不叠加到 166，也不算真实 FFmpeg/CUDA/COS 验收 |

独立 QA 的实际合并命令：

```powershell
python -B -m unittest scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_upgrade scripts.test_drama_youtube_unified_rpc scripts.test_drama_youtube_canary scripts.test_drama_youtube_three_table_rehearsal -v
```

5 项媒体对抗分别为：片头丢失（5.021016→3.966667 秒）、音频补齐掩盖视频截断、长片少 1 秒（7200→7199）、视频流时长 NaN，以及正常舍入对照（5.021016→5.000000）。前四项均拒绝坏产物、清理失败输出、不计算最终 SHA、不进入上传；对照正常返回。FFmpeg、ffprobe、文件写入及上传均为 mock；证据保留在独立 QA 任务工具输出，未生成单独落盘报告。

### 本轮已验证的代码行为

- 固定频道、单 operation、真实已完成 job/source 绑定，普通 worker/outbox 与 canary 隔离；重复操作员、重复 CLI 不创建第二个上传会话或评论。
- 上传前持久化 session intent；未知结果只对账原会话/视频；没有身份的未知上传与未知评论阻断，不盲重试。
- 公共和 canary 评论均传入冻结 `channelId`，只接受响应 `snippet.topLevelComment.id`；2xx 但缺失/身份不匹配仍为 unknown。依据 [commentThreads.insert 官方合同](https://developers.google.com/youtube/v3/docs/commentThreads/insert)。
- 任何 claim/OAuth refresh/upload 之前，先验证鉴权 RPC health 的固定 writer 身份、主库可写、精确 schema/index/grants。仅配置 executor 不算健康通过。
- 每条 canary outbox claim 之前重新读回 processed/succeeded/unlisted；包括已完成任务遗留的 pending 重试。读取失败或隐私漂移会持久化 hold，0 新 outbox claim，不重发已确认评论。
- 三表快照/恢复脚本的端点、容器、schema、数据盘、凭据权限、候选及文件 SHA、数据/结构/索引不变量与证据时效均有离线回归。

### 历史真实环境状态（c719 阶段；旧库迁移路线已退役）

此表只记录旧候选的当时状态。后续用户已改为 ads_ai 新建专用表，现已建表成功，不再以 kunlunads_dev 无写权限或旧三表迁移/备份作为当前门禁；当前门禁为最上方实际 1410 账号创建权限。

| 项目 | 已确认 | 尚未完成 |
| --- | --- | --- |
| HK dark 环境 | e1f5a1d 真实 auto/manual 共 4 个产物通过；两随机成片 720×1280 High、5 秒/150 帧、完整解码；封面回调、即时重复 POST、服务重启重放与 manifest SHA/mtime 不变全部 PASS | 当前仍使用隔离 COS 前缀；正式前缀激活和 CPU 切入尚未完成，不等于完整生产发布 |
| Demucs 真实 CUDA 专项 | 专用用户、mdx_extra_q 四模型：1 秒静音输出 44100 帧/peak 0；2 秒反相立体声输出 88200 帧/peak 0.0079345703125，均 finite；非假模型 | 专项与上行 HTTP 全链路验收分别记录，不重复计数 |
| CPU 主应用 | 仍保持旧 `18787`，未切流；新 `18788` 为隔离路径 | 主应用发布/切流和业务读回 |
| 三表数据保护 | SSH 真实一致性 snapshot + CPU 本机隔离 MySQL `5.7.44` 恢复/迁移演练 PASS；299309 行 | 该结果仅证明本次三表范围，不能替代合法生产迁移账号，也不是全集群灾备；详情见 [迁移文档](migration.md) |
| 生产数据库/RPC | 当前 `ads_aius` 对 `kunlunads_dev` 只有 SELECT/SHOW VIEW | 无合法 admin/migrator/writer，阻塞生产 DDL、健康 RPC 和真正 YouTube 测试；不得复用只读账号绕过 |
| 指定频道测试 | 已授权且只读唯一定位 | 0 真实上传/评论；待全部门禁通过后精确 CLI canary，最终 processed/unlisted、评论和三表读回仍待验 |

主代理经 SSH 完成的真实快照目录为 `/mnt/data-disk/drama-youtube-rehearsal-20260827a11c0001/snapshot`，绑定候选 `c719bebf72be900ec3853858dc53b36b83beffd2`；视频/评论/日志分别为 244151/53/55105 行。机器生成证据如下：

- `snapshot-manifest.json` SHA-256：`426685eda5041d332cde8f70ca724a7bbc3ae6038a0da6d02d1fabc2233f0603`。
- `rehearsal-result.json` SHA-256：`0178a8b633c6433cffca4be32cdb4b5adfaa47e63bcaafb1398d847455d7d43b`。
- `backup-evidence.json` SHA-256：`36579d5ed7a2234d821638b3644c4b32ce024354cbdc136aa97b53dbc3fe9dec`。

证据类型是 `table_snapshot_rehearsal`，不是 Tencent API 云备份或 CynosDB 全集群灾备证明。证据仍绑定 c719beb；后续生产 apply 前重新核对时效和候选，不改绑到 HK e1f5a1d 或 docs-only 提交。

HK 最终报告为 `/data/drama-synthesis-gpu/work/acceptance/http-media-20260827-v3.json`，SHA-256 `40746316694eeb4d34fb4511713acb13b8de14cff382f6116fdd99f1351f2175`；auto/manual job 分别为 `309b8450f03fd01de853cf4fa8b184ed`、`8474911734767ff621d9ddcdd7363565`。重启重放时未运行 fixture HTTP 服务，两 job 均返回 200，manifest SHA/mtime 未变，未重建工作目录。主流程同时验证渲染繁忙 503、health/catalog 可用及封面回调 200。4 个产物 SHA 和服务 PID 见 [HK 实测记录](hk-gpu-setup-20260827.md)。

独立 QA 只读核对该 JSON 的 SHA/配方/产物映射，并查看每个随机视频的 0.5/1.1/3.1/4.8 秒共 8 帧：片头保留、两段顺序正确、末段与模板继续变化，抽样未见冻结。该次没有操作服务器或重跑测试；503 和 1.710 秒仍引用主流程 SSH 证据，不伪称独立真机复测。

### 缺陷与发布建议

BUG-013/014 与 canary 两项 P1（BUG-018/019）的代码修复已通过独立复验；BUG-012/015/016/017/020 的运行环境、模型、滤镜线程、时间轴和缓存问题已由最终 HK 实测闭环，BUG-021 缺失 profile P2 已修复并独立复验。见 [缺陷索引](bugs/README.md)。独立最终代码/证据评审无新增阻断，但尚未执行 CPU 生产/YouTube 集成，不宣称这些未测环节没有缺陷。

缓存增量已推送、部署 e1f5a1d 并通过实际双模式及重启幂等验证。独立 188 项首次 187 PASS，1 项迁移文档真实错误字符串补回后定向复测 PASS；27 文件语法/AST 与 diff PASS。实现者 focused 22 例和旧 166 例均包含于该范围，不叠加；CPU 候选和三表演练证据仍绑定 c719beb。

取得目标库合法管理员或最小权限 migrator/writer 配置后，可继续已授权的生产部署；已有三表证据在 apply 前须重新核对 SHA/时效，不能重复覆盖已完成演练目录。不得提前打开正式 live/sync、切 CPU 流量或执行 public 测试。真实 canary 的用户授权已经具备，当前阻塞是数据库账号权限，不是“用户尚未授权测试”；随后仍须完成正式前缀激活、CPU 运行与真实发布验收。

## 历史：2026-08-26 Wave8 与线上实查增量

以下为旧候选 `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883`、`2b26b540660fd3687fa7c66e68a246d1a706136a` 的历史证据。它们不包含次日发现的运行包、媒体时间轴、canary 与恢复演练缺陷；旧授权边界已由上文取代。所有历史测试数均不得与 166 相加，也不能作为最新 SHA 的重新验收。

### 当日独立 QA 证据

2026-08-26 对 exact SHA 执行：

- focused 45/45 PASS；broad 77/77 PASS；实际 Chrome Playwright 3/3 PASS。
- compile 11/11 PASS；Python 3.9 AST 11/11 PASS；browser spec syntax 1/1 PASS；inline JS 4/4 PASS。
- unified writer：3 个正常实体合同 + 26 个 adversarial 合同用例全部 PASS；outbox malformed/fencing 9/9 PASS。
- hostile recipe 的 img/onerror/script/quotes 以文本可见，0 执行、0 DOM 注入；两 UI mirror 一致。
- 未发现 candidate P0/P1。旧候选 `f05e10f`、`2df9aef`、`d27c82c` 均为 HOLD/obsolete，不可替代 Wave8 SHA 作为发布候选。

### 当日实现者补充证据

- focused 45/45 PASS；相关 broad 116 collected：115 PASS、1 个 Windows POSIX permission 预期 skip、0 failure；实际 Chrome Playwright 3/3 PASS。
- 本地 py_compile 10 个文件、HK Python 3.9.6 stdin-only runtime compile 9 个文件、browser spec `node --check`、两 HTML 6 个 script block parse、static mirror、staged diff/secret/scope/artifact 检查均 PASS。
- 全部外部动作使用 temp/fake；未执行真实短链 writer、YouTube 上传/评论、统一 MySQL 写入、CPU/HK 部署。

### 当日发布结论（已被 2026-08-27 更新取代）

当日 Wave8 与线上实查后的代码增量通过独立 QA，但并非 production release PASS。gy `/s2l/youtube` app-owned root、Nginx 隔离路由和 X 兼容性检查完成；统一三表迁移、账号/RPC 与固定 public 合规风险留待后续。当日尚未取得真实 YouTube 精确授权；2026-08-27 已取得上述指定频道一次 unlisted 测试授权，不再沿用该旧判断。

### 当日线上实查后的增量证据

- X 渠道现行机制已核对：先在 SQLite `x_post_publish_log` 预留自增 ID，以该 ID 生成 `https://gy.g2flow.com/s2l/<id>.html`，冻结 long/short URL 和正文，再原子创建不可覆盖 wrapper，成功后才进入 X 发布；抽样 ID `633` 的数据库 long URL、数字文件名与 HTML canonical 一致，现有短链返回 200。
- YouTube 不创建新域名、DNS、证书或 server block；只复用现有 `gy.g2flow.com`，增加优先级更高的 `/s2l/youtube/<数字短码>.html` 隔离路径。CPU 已建立 `drama-youtube` owner/root 与 Nginx snippet；`nginx -t` PASS，X `/s2l/633.html` 仍为 200，不存在的 YouTube 数字路径为 404，POST 为 403。未生成真实 YouTube 短链文件。
- 统一三表确认已存在于 `kunlunads_dev`：`ads_youtube_videos`、`ads_youtube_comments`、`ads_youtube_publish_log`。当前应用账号只读；增量实现提供固定白名单 loopback RPC、独立 0600 DB/RPC 凭据、三表完整 legacy 字段映射、负数 synthetic queue join，以及 external-id nullable 列/唯一索引的可审计迁移脚本。
- 首次增量独立评审结论为 HOLD：发现 writer 18836 与现有 FB 隧道硬冲突，以及 migrator/runtime 权限、精确 schema/grant、credential owner/0600、ACL 与共享库回滚合同缺口。该结论阻止了提交和部署。
- 修复候选改用经 CPU `ss`、线上配置和仓库三方核验为空闲的 18837；新增可复现 writer env、一次性 migrator、长期最小权限 writer、全量 schema/grant fingerprint、fresh backup evidence/rehearsal、exact owner/0600、短链 ACL 检查和安全回滚。实现者 Python unittest 91/91 PASS；实际 Chrome Playwright 3/3 PASS；CPU Python 3.9.6 对七个运行文件 compile PASS；线上只读 45 个 legacy 列 fingerprint 与 ACL `--check` PASS；`git diff --check`、changed-file secret scan 0 PASS。
- 线上 MySQL 已只读确认是 `5.7.18-cynos-2.1.14-log`、`@@read_only=1`、账号 host 为 `43.166.187.96`、`information_schema.ROUTINE_PRIVILEGES` 不存在、`SHOW GRANTS` 使用单引号账号。候选因此不查询不存在的表；USER/SCHEMA/TABLE/COLUMN 由 information_schema 精确闭包，routine/proxy/未知授权由 `SHOW GRANTS` 白名单拒绝。
- 第四轮最终提交前独立复审 PASS，P0/P1/P2=0/0/0；focused 46/46、RPC/migration 7/7、MySQL57 grant matrix 8/8、related broad 115 PASS + 1 预期 skip、CPU Python 3.9.6 compile、diff/secret/artifact 全部 PASS。随后对 immutable code SHA `2b26b540660fd3687fa7c66e68a246d1a706136a` 再跑实现者 unittest 91/91、Playwright 3/3、CPU compile 7/7，全部 PASS。
- 上述代码 QA PASS 允许进入生产门禁执行，不等于 production release PASS，也不授权真实短链、MySQL DDL/写入或 YouTube 发布。
