# 发布、验收和回滚合同

更新：2026-08-28。**生产尚未切换；本文是受门禁约束的操作程序，不是执行记录。** 当前分支为 `codex/drama-synthesis-reliability-20260828`。发布候选最终SHA、备份位置、性能决定和生产读回证据由主任务写入 [test-report.md](test-report.md)，本文件不填未执行的PASS。

## 1. 基线与不得变化的边界

| 对象 | 已确认设计记录的基线 | 发布前必须重新取得的事实 |
| --- | --- | --- |
| CPU源码 | `420957be4c288308c38b97f773be330208887204`；app.py SHA256 `782ce12cf17890b1efd6d834e53a14debb5747fec7fe143794eaa34705f0278d` | `/root/drama_material_service` 实际代码/配置/静态文件漂移；不能推断全目录都等同该提交 |
| HK GPU | `43.154.250.89:/data/drama-synthesis-gpu/current`，记录版本 `e1f5a1d04cfb510df9c2444ac592adec2827508b` | current实际链接目标、版本/文件哈希、服务环境及进程 |
| CPU服务 | `43.166.187.96`，API与 `drama-material-job-worker.service` | 正式任务、租约和新任务来源，确认维护期间不再取新制作 |
| GPU服务 | `drama-synthesis-gpu-worker.service`，loopback8787，CPU既有隧道18788 | 正式制作队列/子进程为空；既有FB等服务健康 |
| 资源 | CPUQuota=200%、Nice=10、TasksMax=128、重制作并发1 | 实际值及磁盘/内存余量；未获长片证据前维持这些配置 |

原case任务 `679e7c49acbf4af79f78bf60d76c5dd7` 已在约17:36:51自行退出，17:41结果核对确认仅封面、无视频及完成manifest；服务未重启、无OOM证据。旧10800秒渲染超时为时间证据支持的推断，未找回原异常，见 [原任务核对](evidence/original-case-outcome-20260828.json)。未执行终止操作，原CPU failed状态保持；不得盲目重制或写成完成。长片内存缺陷仍未关闭，见 [BUG-002](bugs/BUG-002.md)。

迁移任务 `gpu-service-migration-20260828T1502` 仍在执行；截至本文件更新时，**新的HK媒体窗口尚未明确释放**。在迁移任务另行明确许可远端隔离代码测试前，只做本地、无网络、无媒体的代码测试；即使取得无GPU代码测试许可，仍不得在HK启动FFmpeg、下载压测或COS写入，也不沿用2026-08-28的旧窗口。CPU公共目录/主API及HK剧集上线必须在发布前再次协调目录、端口和窗口；测试窗口不等同生产切换许可。每次渲染或切换前仍须重新证明正式任务排空，旧PID退出不代表之后不会有新任务。

现行轻量健康检查是CPU侧 `GET http://127.0.0.1:18788/healthz`（HK侧8787），无需鉴权，预期200及`ok=true, role=media-only`。`/health`不是有效路由。素材catalog `/api/gpu-video/random-overlay/catalog` 需要既有Bearer Token，不打印该值；轻量health通过只证明HTTP入口存活，不证明媒体或COS已验收。

保留现网素材复制通知、输出预览过滤和YouTube弹窗等后续改动。只发布本期涉及代码，不重启或改写FB/X/TT/YouTube发布服务，不调整Token、机器时间、short-link及资源目录。

候选570c1bd的15个变更运行文件已按Git blob字节冻结 [文件清单及SHA256](evidence/runtime-file-manifest-570c1bd.json)，供CPU非Git部署目录及GPU版本目录逐文件核对。它不包含私有输入、数据库或生产配置，也不是部署完成证明。后续候选若改变运行文件，必须重新生成并验证；仅文档/测试工具改变时也要核对上述字节仍一致，不能盲用旧清单。

## 2. 配置与存储

| 配置/路径 | 默认或现有值 | 发布规则 |
| --- | --- | --- |
| `DRAMA_GPU_ASYNC_ENABLED` | `0`，代码默认OFF | CPU API和任务worker保持一致；隔离及媒体门禁通过后才改1 |
| `GPU_VIDEO_WORKER_URL` | CPU既有 `http://127.0.0.1:18788`；GPU为空 | 不换隧道/端口，不让GPU转发回CPU渲染接口 |
| `GPU_VIDEO_WORKER_TOKEN` | 既有私密值 | 沿用受限配置；不打印、不入Git、不放命令行参数 |
| `DRAMA_GPU_MAX_CONCURRENCY` | `1`，实现只接受1 | 不提高重制作并发 |
| `DRAMA_GPU_QUEUE_LIMIT` | `8`，允许1～64 | 首次发布维持8；计等待任务，完成查询不占名额 |
| `DRAMA_GPU_DOWNLOAD_WORKERS` | `4`，允许1～8 | 首次维持4；8路须有相同样本4路基线及至少15%收益 |
| `DRAMA_GPU_FILTER_THREADS` | `2`，允许1～4 | 对照只用2/4，未通过90分钟验收不调高 |
| `DRAMA_GPU_SUBPROCESS_TIMEOUT` | `43200`秒 | scoped通用子进程保护；不是HTTP总等待截止 |
| `DRAMA_GPU_RENDER_TIMEOUT` | `43200`秒（12小时），允许60～86400 | 专属模板渲染截止；显式timeout同样受控，不再截成10800秒，不绑定HTTP四小时等待；到期停止自己的子进程并wait确认 |
| `GPU_VIDEO_WORKER_TIMEOUT` | 既有14400秒 | 保留旧同步/封面等兼容用途；异步观察不以此超时重制 |
| CPU轮询 | 连接3秒/读取15秒、间隔10秒 | 当前客户端固定值；不引入另一层四小时future截止 |
| COS SDK重试 | async专用client的retry=0 | 保留其他平台默认；目标1.9.44真实SDK无网络transport测试必须零skip通过 |
| COS桶版本控制 | 2026-08-28只读查询Status缺省，未启用 | 不改桶设置。创建/完成前重查，状态不明或已启用/暂停均停止；条件禁止覆盖头不能用于已启用版本控制桶的保证 |
| GPU工作根 | `/data/drama-synthesis-gpu/work/jobs` | `.runtime`就在此根下，必须持久保留 |
| GPU上传检查点 | `/data/drama-synthesis-gpu/work/jobs/.runtime/uploads/<job_id>/<对象key哈希>.json` | 仅drama异步执行上下文使用，独立于可清理的单任务媒体目录；保留UploadId/阶段和锁文件，不删记录触发新create |
| GPU完成manifest | `/data/drama-synthesis-gpu/results/manifests` | 新写v3必须绑定当前输入指纹及每个产物的bucket/key/SHA/大小/ETag/binding；认证HEAD或本地读回失败不能删记录重做 |
| GPU公开本地产物 | `/data/drama-synthesis-gpu/results/public` | 仅沿用已成功上传后的清理合同；回滚不能额外删除现存产物 |
| GPU concat/去BGM本地完成记录 | `/data/drama-synthesis-gpu/work/jobs/<job_id>/<成片名>.completed.json` | publish前原子持久化并readback，绑定冻结输入、有序片段或concat源、处理profile和成片SHA/大小；已有成片缺记录、记录冲突/损坏或写失败都保留并停止，不删除后重制 |
| GPU本地模板提交记录 | 成片旁的 `.render.prepared.json`、`.render.json` | prepared先保存未完成start guard，验收后升级为绑定源/配方/产物的完整记录；只凭完整prepared恢复rename/final-save，只有guard或未登记旧输出不得自动重制/收编 |
| CPU业务SQLite | `/root/drama_material_service/data/drama_material_jobs.sqlite3` | 先在线备份并校验；回滚保留新表与状态，不恢复旧快照覆盖新结果 |
| `DRAMA_GPU_TIANMAI_CDN` | `original`；仅接受original/international | 只影响CPU新冻结任务；已有样本结果分化，首次保持original。international须另获同源/缓存条件对照收益，不得改旧任务身份 |

环境文件分别沿用GPU `/etc/drama-synthesis-gpu/worker.env` 和CPU现有受限文件/覆盖层。通过systemd `EnvironmentFile` 加载，**不要在shell中source私密环境文件，也不要打印完整Environment或进程环境**。只读回本表中的非敏感配置项。

CPU新增SQLite表 `drama_material_job_remote_runtime`，字段包括job_id、fingerprint、payload_json、snapshot_json、generation、resume_requested_generation、first_started_at及UTC审计时间。建表/补字段是增量且幂等；先在备份副本验证，不做破坏性迁移。GPU只使用私有JSON，不初始化业务DB。源URL所在冻结payload及其备份必须受限保存。

异步COS上传由 `features/drama_synthesis/cos_upload.py` 实现单线程持久分片；其他业务调用保持旧合同。恢复须校验本地SHA/大小、目标及分片编号/长度/ETag与本地MD5；完成对象也要由认证HEAD匹配本次上传标识、SHA元数据、大小元数据、Content-Length及已保存完成记录，**不允许只凭公开HEAD长度复用**。新v3 manifest逐产物保存bucket/key/SHA/大小/ETag/binding，且强制保存当前64位输入指纹；异步读取禁止从公共文件名推导URL。无可信检查点但同key对象已存在时停止冲突处理；create结果未知时保留`creating`检查点等待人工核查，不另建UploadId、不自动abort、不删除本地成片。complete丢响应只能按绑定元数据对账，不能假定失败后重新上传覆盖。

发布前由目标服务账户预创建并核对工作根、`.runtime/jobs`、`.runtime/locks`、`.runtime/uploads`、results、manifests和public目录：不得是符号链接，属主/模式须与unit一致，工作及结果目录可写，素材目录只读。首次创建目录及新manifest的Linux持久顺序必须覆盖父目录fsync；预检应在同一文件系统写入、readback并删除一个非媒体探针文件。该探针只验证权限和持久写路径，不得清理既有账本、检查点或成片。

concat和去BGM成片在任何publish前先写并readback各自的本地完成记录；恢复时严格校验工作区成片，公开副本缺失只从该成片原子恢复后续传。不得因公开副本/上传响应丢失再次concat或执行Demucs。模板渲染先写start guard，再在输出通过原规格/时长校验、SHA/大小确定且fsync后提交完整prepared；rename或最终记录写入失败时保留并复原。若任一完成记录自身未能落盘，保留临时成片和guard并停止自动恢复，不以删除它们换取重渲染。有效记录复用和这些保护都不等于长片内存缺陷已关闭。

## 3. 候选推送与生产切换分级门禁

1. 先完成本地代码审查、核心及最新媒体/COS边界回归、旧升级/目录回归和页面检查；记录真实命令与文件哈希。测试计数以主报告为准，旧计数不能替代修订后重跑。
2. **本地通过后先提交并推送GitHub候选，记录完整40位SHA**；只按评审后的路径清单精确stage，并用cached name/status逐项复核，禁止 `git add .` 或 `git add -A`。仓库根的 `output/` 含私有SDK、fixture、配方和页面快照，永不stage或commit，也不得为clean/候选门禁删除、移动或清空；服务器验收必须使用全新checkout。服务器从GitHub将该SHA取到新的隔离目录，保持生产current、unit及开关不变。这一步为隔离测试取得代码，不是生产发布，不要求长片先于候选推送完成。若修复改变代码，须重新本地验证、推送新SHA并绑定新的隔离证据。
3. 在该SHA的Linux全新隔离目录跑同套测试和真实短子进程跟踪，验证owner/job锁、/proc PID/boot/进程组、Popen启动窗口、磁盘失败、停止接新和恢复。测试需满足资源/权限前提，不连接业务发布路由；拉取代码不等于已获GPU制作许可。
4. 在维护排空后，用隔离任务ID、工作/结果目录和COS前缀演练提交丢响应、CPU接管、成片manifest读取、实际分片上传/丢响应恢复及事务回填；核对认证HEAD绑定元数据、creating未知结果保护、上传完成重放，不多一次渲染或create。通知故障不回到媒体制作。
5. 固定配方先短样后5400～7200秒长样：完整解码、时长/音画/片头/集边界/模板动画和资源曲线合格。单测、5秒视频、成功返回码都不能代替长片验收，具体见 [媒体验收手册](media-acceptance-runbook.md)。
6. 下载和CPU候选分开测量；任何性能结论只对记录的源、配方、配置与范围成立。没有合格证据则维持下载4、2核/滤镜2。
7. `img.tianmai.cn` 与 `accelerate.tianmai.cn` 同资源、同并发、同样本对照，更快且校验无退化才启用。前缀SHA/总长度只证明抽样边界，完整对象一致须另有受控对照。启用策略仅作用于新冻结输入；不复用另一来源的`.part`身份，无换源收益证明时保持original。
8. 全部适用门禁通过后，与迁移任务取得生产维护窗口并再次停止接新、核验排空。原任务本轮已核对无成片，保留failed和输入，不将自行退出写成done或借发布触发重做。其他在途任务必须自然排空；如需终止须另获明确授权并核查后续状态。无法确认即停止切换，不强制解锁。

## 4. 隔离性能工具

在候选代码目录使用目标运行时执行 `scripts/benchmark_drama_synthesis_media.py --help` 查看当前参数。工具要求显式 `--apply` 和全新的绝对输出目录，只写隔离证据，不提交生产任务或上传COS。真实下载、prepare、render或decode的任何 `--apply` 都必须先取得迁移任务对该动作的新窗口许可；未获许可时下列命令仅供评审，不能执行。

```text
python scripts/benchmark_drama_synthesis_media.py --apply download --url-file <私有URL数组JSON> --output-dir <新的绝对证据目录> --workers 4 --bytes-per-source 16777216
python scripts/benchmark_drama_synthesis_media.py --apply download --url-file <同一私有URL数组JSON> --output-dir <另一新的绝对证据目录> --workers 8 --bytes-per-source 16777216 --four-worker-evidence <成功4路证据JSON>
python scripts/benchmark_drama_synthesis_media.py --apply download --url-file <候选域名URL数组JSON> --output-dir <新的候选证据目录> --workers 4 --bytes-per-source 16777216 --compare-evidence <同并发原域名证据JSON>
python -I -S -B scripts/run_drama_media_acceptance.py --candidate-sha <精确40位SHA> --run-id <新批次> --sample-kind long --config 2c2t --trial r1
```

尖括号是必须来自本次冻结记录的参数，以上不是已运行命令。下载工具保持普通显式CLI；渲染不得直接调用其 `render` 子命令，必须由固定媒体launcher创建并验证200%/2线程、400%/2线程或400%/4线程的隔离unit。launcher缺省只预览；prepare、render、decode的最终参数和调用顺序以同一候选SHA内的 `--help` 及 [媒体验收手册](media-acceptance-runbook.md) 为准。正式入口仍须先排空；固定锁只防验收批次互相并行。每次记录launcher、`evidence.json`、`process-samples.jsonl` 和解码结果；`ok=true` 不代表用户视觉验收通过。当前 `/data`仍在根文件系统，运行前用 `findmnt -T`、`df` 核验空间，不自动迁盘或无限缓存。

真实COS恢复验收只使用 `scripts/verify_drama_cos_upload.py`，缺省为零网络预览。真实执行必须用固定运行时的 `python -I -S -B scripts/verify_drama_cos_upload.py --apply ...`，并绑定同一干净GitHub候选、全新 `cos-...` 批次、独立非敏感MP4（大于16MiB且不超过256MiB）、全新0700证据目录，以及只含 `COS_SECRET_ID`、`COS_SECRET_KEY`、`COS_BUCKET`、`COS_REGION` 四项的当前用户自有0400/0600专用文件；未知赋值、环境继承和生产COS前缀一律拒绝。clean门禁必须同时拒绝tracked工作树改动、staged改动、untracked、ignored及skip-worktree/assume-unchanged，Git以精确40位commit/tree运行并禁replace refs、全局/系统配置和local fsmonitor；Git二进制root-owned且不可组写/他写，关键候选文件逐字节匹配Git blob。COS SDK 1.9.44及requests/urllib3/certifi/idna/charset-normalizer等真实传输依赖须在任何package导入前证明来自root-owned只读单一依赖树；树内symlink、`.pth`、`.pyc/.pyo`、`.egg-link`或任意组/他写路径均拒绝。目标固定runtime若已有这些文件，验收应安全失败并另行准备受控依赖树，不能删除生产runtime文件来绕过。ffprobe的stdout/stderr在运行期间分别有硬上限，超限、KeyboardInterrupt或读线程异常均kill并wait，且仍未读取凭据。脚本内部固定总期限3600秒，外层独立unit固定 `RuntimeMaxSec=3660` 兜底，不提供CLI或环境变量放宽。创建和完成前各读取一次V2通知配置，必须严格为空；创建前和完成后匿名HEAD必须均为403，完成后还要读取对象ACL并拒绝公开/组授权。任一读回不明即保留证据、UploadId、对象或分片并停止，不abort、不删除、不换前缀自动重试。通过还须证明第一次分片成功响应丢失后复用同一UploadId、Complete成功响应丢失后由绑定HEAD对账、完成重放零写入，以及完整认证下载SHA等于源文件。该验收会真实写入一个新私有COS对象，必须另获COS写入窗口；不触发业务API、通知配置写入或媒体渲染。

## 5. 已验收GitHub候选的生产切换步骤

1. 只提升第3节已推送GitHub、并通过全部适用隔离/长片门禁的精确SHA；核对受测SHA与拟发布SHA一致。候选推送已在隔离测试前完成，不能混淆为此时才首次推送，也不能测试A却部署B。
2. 记录CPU实际变更文件哈希、GPU current实际链接目标、unit/drop-in与非敏感配置；保存只读源码/配置回滚包。SQLite采用在线backup API备份并做完整性检查，不直接复制运行中的WAL组合。
3. 备份并校验GPU `.runtime`（含独立`uploads`）、完成manifest、render start guard/prepared/final记录和现存产物索引；受限保存含源URL的账本。大媒体对象不盲复制耗尽根盘，不删除COS对象或分片。
4. 门禁确认正式制作排空且不再接新后，停止CPU材料任务worker取新；若发现GPU仍有活动或未知进程则等待并重新核查，不因发布而切代码/kill进程。另行授权终止需先按第3节处理并确认，本文不授予该权限。保留业务查询能力，记录窗口中入队任务。
5. GPU从精确候选建立版本化源码目录，沿用已核验依赖、模型和素材。先以隔离根进行语法、导入及运行时预检，确保只读素材与可写work/results权限正确。预检失败不动current。
6. 在GPU空闲后将current原子指向候选源码；只重启 `drama-synthesis-gpu-worker.service`。验证版本、health、认证查询和异步/兼容接口；新测试制作只能在已批准隔离环境，不对正式任务POST探测。
7. CPU从同一GitHub精确候选发布，保留已核对的线上差异；API与worker初始均保持 `DRAMA_GPU_ASYNC_ENABLED=0`。验证业务查询、目录、旧输出预览及权限，不能把新接口上线等同开关开启。
8. GPU兼容接口和隔离全链路通过后，在CPU两进程一致设置异步开关为1，按维护窗口只重启需要的API/材料worker，恢复取新。只观察自然到来的任务，不制造真实测试发布。
9. 记录每次切换/重启时间、进程健康、首个自然任务的job/指纹/代次、阶段变化、最终产物对账和错误率；提交用户人工检查页面与成片。未获人工验收保持待验状态。

## 6. 切换后检查与停止条件

确认API/worker使用同一开关、同一冻结payload；同job重复查询不会出现第二个渲染进程；CPU更新频率约10秒，页面总耗时持续增长，封面单列完成；成片/配方与CPU done一致且完成通知不导致重做。上传重放必须使用相同检查点/UploadId并核对绑定对象元数据，不能只HEAD长度判成功；只有render start guard或未知creating时维持核查，不删记录恢复。回读不得泄露Token、源URL、COS凭据或完整数据库内容。

出现未知存活进程、指纹/配方/检查点冲突、磁盘不足、连续网络错误、CPU状态倒退、其他平台资源显著恶化、长片效果不合格时停止接新并保留证据。先诊断，不能以删除账本、清租约、重复POST或放大并发“修复”。

## 7. 回滚程序

1. 暂停接新，先查询并保存所有新异步记录状态。GPU queued/running/recovery_required未对账完时，不把其业务任务交回不了解新账本的旧worker。
2. 能自然完成的保留当前观察与结果回填直到完成；状态不明的保留账本/检查点并转人工核查。不得通过改CPU开关、恢复旧DB或删记录强制释放。
3. 队列排空或已受控隔离后，将CPU异步开关恢复为0，回退到本次部署前的真实源码/配置快照。`420957b`只是源码比对基线，不能替代实际线上回滚包。
4. GPU空闲后把current原子切回本文件第5节步骤2记录的实际旧目标（预期版本e1f5a1d，须以现场备份为准），还原本次确实改动的unit/drop-in，窄范围重启并做健康/缓存只读验证。
5. **保留CPU数据库及新增表、GPU `.runtime/uploads`等账本、完成manifest、现存成片、render start guard/prepared/final记录、COS对象和已有分片。** 不降级或清空状态，不恢复旧快照覆盖已成功制作的结果；旧代码若不能理解残留未完成账本，保持材料worker停接新并核查，不abort或新建UploadId绕过旧记录。
6. 记录回滚时间、源码/配置哈希、保留任务清单和产物一致性，再恢复正常入口。单独撤销未经证实的域名/并发/CPU配置，不影响已冻结执行身份。

本次文档更新仅完成本地合同和检查，没有远端部署、生产重启、候选参数启用或人工代验收。
