# 后端终审与独立复测记录

## 范围与结论

2026-10-22，独立工作树 `D:\codex\worktrees\video-editing-v1-20261022`。初次终审核对权限隔离、批次 / 版本 / 成片账本、上传未知结果恢复、CPU 存储边界；该阶段只新增本文，不修改功能代码、不执行服务器变更或故障注入。初次记录时工作树 HEAD 为 `c39cce6e54a6d658e1dacae9e7a503d7bb8f4089`，恢复验收辅助脚本与测试尚有待提交文件；测试针对记录时工作树。后续获准完成的真实隔离重启和上线只读核对见文末补充，不能把两个阶段混为一次未变更检查。

本轮60项后端与辅助工具子集全部通过，无失败、无跳过；其中真实COS SDK签名构造测试使用虚拟凭证和固定时钟，不调用COS。后续BUG-006修复后，后端负责人和主实施分别执行完整Python117项（116通过、1项可选COS SDK跳过）；另有JS23项通过、旧模块215项（209通过、6跳过），子集不另行叠加。同版本跨刷新重提重复批次问题已完成本地修复、新增9项回归及独立只读审查，部署及线上回读待主实施。权限测试基于可信会话桩，不能替代多人实会话；GPU合成素材成片、隔离完成态恢复及正式UI进展见后文，不计入本轮60项。

## 权限与来源边界

| 核对点 | 当前实现与证据 |
| --- | --- |
| API 模块权限 | `routes.handle` 在读取数据库与请求体前调用 `_require_cookie_module('video_editing')`；写请求另经过 `_require_same_origin_json`；最多接收 2 MiB JSON |
| 可信操作者 | user_id、tenant_key、name、admin 来自已有可信 Cookie 会话；浏览器 JSON 不能指定实际 owner 或 admin |
| 上传与个人对象 | owner 为 tenant_key + user_id 的规范化 JSON；upload / project / task 按 owner 或可信管理员判定，不仅比较 user_id；越权个人对象返回 404 |
| 共享库 | 有模块权限的成员可读 ready 素材；未 ready 素材只供上传者 / 管理员查看；改名、软删除仅上传者 / 管理员 |
| 目录来源 | 新来源仅来自既有产品 / 剧集目录；episode source 读取重新核对当前可用产品；草稿不能传任意 URL、借用其他剧集或引用另一个素材库视频 |
| 冻结任务 | 已接受任务按自身权限读取 / 恢复；后续产品变化、项目编辑或素材软删除不改写已冻结配方 |

共享 ready 素材跨普通操作者可见是确认的产品行为，不视为个人项目权限泄漏。当前产品目录是既有产品可用范围，不引入已退役模块规则；没有声称存在额外逐产品用户授权体系。

## 稳定批次、版本与成片

`Service.render` 在单个 SQLite `BEGIN IMMEDIATE` 事务中查找既有版本批次、保存调用操作映射，并建立 `ve_project_version`、`ve_batch`、逐剧 `ve_task` 和 pending `ve_output`。任何一部剧时间线无效会整体回滚，不留下半个批次。

- `ve_project_version` 以 `(project_id,version)` 唯一，保存提交时 Draft；每个 task 另保存规范化来源身份、裁剪信息及完整 GPU 配方。
- `ve_batch` 保留 `(owner,operation_id)` 唯一键，保存稳定 `vb_`、项目版本 ID 与原 task_ids；新批次内部操作号由调用者和外部操作号计算。`ve_render_operation` 以调用者owner和外部operation_id唯一，映射到原项目、版本和批次，同操作跨项目 / 版本返回冲突。
- 新operation_id按 `(project_id,version)` 复用最早接受的批次，任何任务状态都不新建或重置。历史原operation_id优先返回自己的原批次，既有重复批次不删除、不归并、不改写账本；`BEGIN IMMEDIATE` 串行化并发查找与建立。
- 新批次 / 任务 / 成片归项目owner，batch与task的 `submitted_by` 记录真实提交者。历史管理员任务保持原权限，项目owner若无权读取旧task，复用返回409 `batch_task_permission_conflict`，不会泄漏任务或另建重复批次。
- `ve_output` 与 task 一一对应，`vo_` 从稳定任务 ID 派生；提交时 pending，经过 GPU 回执身份 / 配方摘要 / COS 对象校验后，与 task completed 在同一事务写 ready。
- 同一已接受批次重放可以在当前项目后来修改、素材软删除后取回原任务；成功任务不能通过 resume 重制。
- 当前没有独立批次 / 成片查询路由，入口仍为 render 返回和 task DTO；后续外部处理应引用 output_id，并核对成片 ready 状态，不以临时 URL 作为身份。

数据库只做附加建表。本文未执行历史生产任务迁移；没有把当前草稿推断为旧任务从未保存过的完整项目版本。

## 上传未知结果与恢复

分片签名包含服务端生成的精确对象 key、UploadId、partNumber、Content-MD5、Content-Type 和精确字节上下界；同一分片不能重签成不同 MD5。完成时由服务端 ListParts 校验完整序号、数量、大小、ETag/MD5；随后读取对象 nonce、大小、ETag 和私有 ACL。没有根据客户端“已成功”直接入库，也没有把 multipart ETag 当成整文件摘要。

| 状态 / 事件 | 已验证行为 |
| --- | --- |
| 初始化响应丢失且 COS 有唯一精确 key 会话 | 原 operation_id 返回同一记录；reconcile 接管原 UploadId，不重复初始化 |
| 完成响应丢失、对象已存在 | HEAD + 身份 + 私有 ACL 验证后接受原对象；重复 complete 返回原结果 |
| 完成响应丢失、对象暂不存在但原分片仍在 | 读取并核对原冻结清单后，允许同一 UploadId 重发相同 Complete；不创建新会话 |
| NoSuchUpload 且对象仍无法读回 | 保留 unknown 并等待核实；不猜测成功，不重建对象 |
| 完成 / 取消标记落账后进程崩溃 | 记录开始时间；120 秒后 reconcile 将旧 completing / aborting 转为对应 unknown；活跃预算内不重置到 uploading |
| 合并与 reconcile 并发 | reconcile 不把已进入 completing 的记录退回可写状态 |
| 新接单关闭 | 新会话 / 新 probe / 新 render 被阻止；已存在会话可核实、签名、完成；已上传素材待重新开放后 probe；已接受任务读取 / 恢复保留 |

现有限制：初始化结果未知且 COS 按精确 key 查不到任何 multipart 会话时，记录继续保持 `initializing_unknown`。没有已确认 UploadId 时 abort 会要求先核实；当前没有单独的“安全放弃此初始化记录”操作。这避免盲目再建，但该记录不能由普通用户自行清除。此项应作为后续上传管理改进，不能声称所有 unknown 初始化都能自动恢复。会话 7 天到期后禁止新分片签名，已有对象与历史证据仍可核实；本文没有验证自动清理未完成 COS 分片的后台作业。

## CPU 不落视频核对

核对 CPU 路径为 `routes.py`、`service.py`、`uploads.py`、`catalog.py`、`store.py`、`worker.py` 和 `scripts/video_editing_worker.py`：

- 浏览器分片 PUT 直接发 COS；CPU 只处理创建 / 签名 / ListParts / Complete / HEAD / ACL 等元数据请求。
- 目录只执行有界 SQL，补来源身份时仅发 HTTPS HEAD；没有 CPU 下载视频、ffmpeg、媒体临时目录或本地媒体响应接口。
- Dispatcher 对 GPU 发送冻结 JSON 配方，并接收状态与 COS 结果身份；页面预览 / 下载返回短期 COS URL。
- CPU 本地写入为 SQLite 元数据、锁和验收 JSON；视频下载、探测与编码仅位于 GPU 模块。
- 通用错误日志使用固定文字，未透传含签名 URL 的 transport traceback；验收 CLI 的签名仅经管道交给本地调用者内存，不计入持久证据。

该结论来自代码路径核对与先前真实工作站直传证据，不等同于对整台主机所有业务文件的取证扫描。旧业务存储行为未改变。

## 本轮复测命令与准确计数

工作目录为上述工作树，PowerShell：

```powershell
@'
import sys, unittest
sys.path.insert(0, r'D:\codex\tmp\video-editing-sdk-test')
names = (
    'test_video_editing_routes.py',
    'test_video_editing_uploads.py',
    'test_video_editing_service.py',
    'test_video_editing_store.py',
    'test_video_editing_validation_cli.py',
    'test_video_editing_catalog.py',
    'test_video_editing_recovery_acceptance.py',
)
suite = unittest.TestSuite(
    unittest.defaultTestLoader.discover('tests', pattern=name) for name in names
)
result = unittest.TextTestRunner(verbosity=1).run(suite)
print('COUNT', result.testsRun, 'FAIL', len(result.failures),
      'ERROR', len(result.errors), 'SKIP', len(result.skipped))
raise SystemExit(0 if result.wasSuccessful() else 1)
'@ | python -
```

当时结果：`Ran 60 tests in 2.343s / OK / COUNT 60 FAIL 0 ERROR 0 SKIP 0`。SDK目录是本机独立临时依赖，不在仓库，也不含生产凭证；未安装该SDK的机器会跳过相应可选测试。该60项为BUG-006修复前的历史子集记录，不能与后续完整回归117项相加。修复后的117项由后端负责人和主实施分别执行，116通过、1项可选SDK跳过；其中service29项全部通过，diff检查与Python3.9 AST检查通过。

本轮未重复已通过且实现未变化的真实 COS 上传与目录查询。此前亲自执行的只读目录证据：候选 `9d26935624285687b2197696f6516f8ed553becf`，两个真实剧 ID 精确查询均约 2 秒、剧集读取分别 44 / 57 项且约 3 秒、名称前缀 2 项约 2 秒。EXPLAIN 分别为 `ref/content_id/225` 和 `range/name/527`，没有全表 scan。CPU 安装 dry-run passed，计划 26 文件，未 apply / 未重启。证据位于 CPU `/mnt/data-disk/video-editing/validation/canary20261022_a/` 的 `catalog-read-validation.json`、`catalog-index-plans.json`、`cpu-installer-dry-run.json`。

## 初次终审缺口与当前进展

以下逐项区分初次缺口和后续证据，不因本地60项子集通过而自动视为全部完成：

| 范围 | 当前进展与限制 |
| --- | --- |
| CFR修复后的真实GPU出片 | 合成素材两条独立时间线已通过真实COS / GPU、完整解码、逐帧内容和音频区间核验；正式双真实剧出片仍进行中 |
| 正式登录页面全链路 | 真实剧集探测、裁剪 / 切割 / 删除 / 撤销 / 重排和双剧提交已执行；Chrome自动文件选择受扩展file URL权限阻挡，浏览器直传 / 中断续传及最终业务成片播放下载未全部完成 |
| COS与临时媒体 | 合成成片HEAD / SHA / 私有性及成功临时目录清理通过；失败媒体保留24小时后的清理没有在本补充中完成实测 |
| 已完成任务重启恢复 | 独立listener在任务完成后停启、GET和重复POST缓存实测通过，详见下一节；不再是“辅助脚本仅完成单测” |
| 下载 / 渲染中断恢复 | 未执行运行中kill注入，也未用真实生产任务补故障窗口；当前完成态恢复不能替代此项 |
| 混合队列与多人权限 | 单槽配置、旧23份账本保持有真实回读；旧新任务同时待执行调度压测及普通有权限 / 无模块权限 / 管理员实会话矩阵未覆盖 |
| 同版本跨刷新重复提交 | BUG-006已完成本地修复，新增9项回归及独立只读审查通过；完整Python117项为116通过、1可选SDK跳过；确切提交部署及线上回读待主实施 |

外部制作接口与推送盈量自定义素材库依然是明确预留范围，本期不执行请求，也不声称已对接。

## 真实隔离重启验收补充

随后在明确授权下执行本地操作驱动 `output/video-editing-validation/isolated_recovery.py --run`，调用 GPU 已发布精确 GitHub release `81f75eca25bd472cddec21ccbd2e09b5f728fe51` 的既有 worker / AsyncRuntime，未修改服务器源代码。临时监听仅 `127.0.0.1:18798`，runtime、媒体、结果、缓存与私有 COS 前缀全部独立，实际共享制作并发仍为 1。

前置检查确认正式入口关闭、CPU原队列无待处理任务、GPU原账本及发布媒体空闲；只暂停空闲CPU claim。隔离任务 `ve_4483dabb553b53369d18aff38f18bd6f` 完成后保存快照，核实隔离监听进程PID / boot / start身份及独立目录后只发送该listener进程SIGTERM，再以同一独立目录重启。执行顺序为“完成→独立listener停启→GET→重复POST cached”，没有停止正式GPU服务。

结果：重启前后及重复 POST 后均保持原 job_id、generation=1、completed_at、配方 / 结果摘要、运行时账本摘要、结果 manifest 摘要及 COS ETag / LastModified / SHA；`output_recreated=false`。输出为私有对象，媒体工作目录已清理。最后确认隔离进程全部退出，CPU claim 恢复原 active 状态，两项发布服务 PID 未变化，线上 GPU 进程及旧 23 条账本未变化。没有执行下载中或渲染中进程故障注入。

证据：本地 `output/video-editing-validation/isolated-recovery-results.json`；GPU `/data/drama-synthesis-gpu/validation/recovery/completed_restart_20261022_a/` 内 `completed-before.json`、`completed-after.json`、`completed-replay.json`、`acceptance.json`。完成态隔离停启恢复已通过，不再属于待验收；运行中断和正式服务中断仍保持上述边界。

## 上线后的独立只读核对

2026-10-22 **13:21:47–13:21:50（北京时间）**，在主实施者完成 CPU 局部导入 / Nginx 路由修复并开放入口后，执行 `python output/video-editing-validation/final_readonly_audit.py`。该脚本只读进程配置、文件元数据、SQLite 与既有 GPU 账本，没有修改配置、任务或服务。此时主实施者正在正式 Chrome 页面验收；以下是该时间点快照，不把后续新任务视为历史账本变动。

| 核对点 | 实测结果 |
| --- | --- |
| CPU 服务 | API PID 3736023、剪辑 worker PID 3737428、旧 claim PID 3734247，全部 active；只发现 1 个剪辑协调进程，dispatcher lease 有效 |
| 旧发布服务 | unified writer PID 2937249、YouTube auto worker PID 2576490，均 active；与隔离验收前身份相同 |
| 权限注册 | live `MODULE_PERMISSIONS['video_editing']='视频剪辑'`；中文码点二次核实正确；`DEFAULT_USER_PERMISSIONS['video_editing']=False`；没有创建用户、授予权限或修改会话 |
| CPU 生产配置 | DB=`/mnt/data-disk/video-editing/state.sqlite3`；COS prefix=`video-editing/v1`；GPU loopback tunnel=`127.0.0.1:18788`；API 与 worker 配置一致，令牌仅核对存在性；enabled=1 且禁用 marker 不存在 |
| 数据盘 | `/mnt/data-disk` UUID=`3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`；根盘 26%，数据盘 66%，数据盘可用 161 GiB |
| CPU 媒体边界 | 扫描该功能数据根（排除 release 源码、Git 与 pycache）：20 文件，无媒体扩展名或 MP4/WebM 文件头；未发现剪辑 ffmpeg/ffprobe 进程。最大 94,031,872 字节文件是已有任务 SQLite 验收备份，不是视频 |
| 旧任务 | CPU `drama_material_job` 仍为 43 条 done；GPU 部署基线 23 条逐文件 SHA 均存在且完全一致 |
| 新任务 | 正式页创建 1 个项目、登记 44 集来源，并选中 2 集探测；生产库 2 个 probe 均 completed、来源 ready，无 error_code；尚无 render / batch / output |
| GPU 服务与槽位 | live release=`81f75eca25bd472cddec21ccbd2e09b5f728fe51`，PID 1526821；健康检查 enabled=true、profile=`video-editing-720-v1`、render_concurrency=1。CPU 仍有持久 lease 和最多一个 accepted/unknown 任务的提交守卫 |
| GPU 账本 | 共 25 条：24 completed、1 failed；相对旧 23 条仅新增上述 2 个正式 probe；busy=0 |
| GPU 临时媒体 | 正式 `work/video-editing`、合成 canary `work/video-editing-canary`、隔离恢复 `media` 和 `cache` 均为 0 文件 / 0 字节；隔离恢复进程不存在，发布媒体活动快照空闲 |

两集成功探测元数据为：`vep_64bcbb263853ae581345c39a112281e2` 327655 ms、`vep_14a75f50aaa2af1b7480423b08571fc7` 171034 ms，均 1080×1920、29 fps、有音轨。主实施者随后确认页面正常轮询后显示时长并成功添加，未发现需要改状态或补任务的故障。

证据：本地 `output/video-editing-validation/final-readonly-audit.json` 与 `final-probe-readback.json`；GPU 旧账本对照 `/data/drama-synthesis-gpu/deploy-backups/video-editing-20261022T050848Z/ledger-hashes.json`。本次没有读取或输出密钥、Cookie、签名 URL，也没有写服务器验收文件。

剩余边界：正式Chrome自动文件选择因扩展未开启file URL访问而受阻，尚不能声称浏览器直传完整验收通过；系统验收标准服务层直传成功见下一节，不能替代浏览器操作。主实施者继续完成双真实剧成片UI验收。多人实会话矩阵、下载或渲染中断的同ID恢复、旧新任务同时待执行调度压测仍未由本补充覆盖。共享槽位配置、完成态隔离停启幂等与旧账本保持有真实证据，但不等同于上述故障或并发场景全部完成。BUG-006同版本重复批次已完成本地修复及117项回归，部署与线上回读待主实施。

## 标准服务层合成素材验收补充

为继续正式页面的编辑 / 出片验收，另获明确授权使用本机既有合成图案 `episode-square.mp4`，通过生产 `get_service(app)`、`uploads.create/sign`、本机直传 COS PUT、`service.complete_upload` 及正常 worker probe 创建一个共享素材。使用固定操作 ID `acceptance_fixture_20261022_a`，操作者明确为 `user_id=system-video-editing-acceptance`、`name=系统验收`、`tenant_key=149bb4a14b57975f`、`is_admin=False`；没有冒充真人、创建用户 / 会话、授予权限、直接写 SQL 或复制其他库记录。

- 素材名称：**验收素材（合成图案，勿投放）.mp4**；source=`va_b1900b8d2345488290116f8059fa4741`，status=ready。
- upload=`vu_831c0cb6279544cab5055724a80bf12c`；重复 complete 保持同一会话成功；正常 probe=`ve_db271bed49d341aba5c95350a6dfb302`。
- 大小 621866 字节，时长 6000 ms，360×360，60 fps，H264 / AAC；GPU 实际 SHA256 与本机一致：`50fc3e7bf6cf8dfce698c2ea6176c132b30a329485ff8fcf955a8a3c373bb23c`。
- COS 对象为 `video-editing/v1/sources/va_b1900b8d2345488290116f8059fa4741/source`，标准完成流程检查私有 ACL，本机匿名 HEAD=403。
- 视频字节仅本机到 COS，CPU 执行既有发布代码的元数据操作；签名仅保留于调用进程内存，没有输出或保存。操作驱动为本地 `output/video-editing-validation/upload_standard_acceptance.py`，证据为同目录 `standard-acceptance-upload.json`。

这项证明标准生产服务、直传、共享 ready 素材和正常探测链路可用，作为后续正式页面选择素材的前置数据；**不能替代浏览器本地文件选择与浏览器直传的验收**。

## 正式双剧项目的裁剪点与冻结版本核对

主实施者从已登录正式 Chrome 提交项目“一期验收 · 两剧独立剪辑”。以其返回的真实任务 ID 定位项目后，只读生产 SQLite 核对（首次精确标题匹配未命中，因为实际标题带“两剧独立剪辑”后缀；未写任何记录）。13:30:39（北京时间）保存的安全快照 `output/video-editing-validation/formal-project-draft.json` 不包含来源 URL。

项目 `vp_44eb51c33d7d4e049fb02afc4f6ad1c5` 第 **16** 版，草稿与提交时保存的完整版本摘要同为 `4c2c16db1dab6da733cd3406d647be95f80d3c8ba2567bd85928cba57e616c1f`。当前草稿、`ve_project_version` 与逐任务冻结配方的逐段控制信息相同，确认统一设置没有覆盖裁剪点。

| 剧目 | 冻结顺序与裁剪（秒） | 转场 / 特殊设置 | 配方预计时长 |
| --- | --- | --- | --- |
| 1wwAOKT9Tz | 素材 0–6；第 1 集 0–2；素材 0–1.2；第 2 集 0–2；第 1 集 4–6 | 前四段结束 fade；最后 cut；第三段 cover + mute，其他 contain + 原声 | 12.0 秒 |
| uWmMdxk9BE | 素材 0–6；第 1 集 0–4 | fade / cut；contain + 原声 | 9.7 秒 |

批次 `vb_df3389c779154302a94f2101511cf456`，稳定版本 `vv_386cb31c43ed220ab3b191abdcd81e9f`。韩剧任务 `ve_1cd8849bcc86492b8ebadbcdb7a440f2`、西语任务 `ve_84fd6aa852844b9a930a5d17176fc8c3`，对应已预分配成片 ID `vo_1cd8849bcc86492b8ebadbcdb7a440f2`、`vo_84fd6aa852844b9a930a5d17176fc8c3`。该首次快照为韩剧 running、西语 queued、两成片 pending；不是成功成片证据，完成与 COS 回执应另行保存。
