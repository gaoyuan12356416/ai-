# TT 香港目标隔离准备

此目录只负责目标配置、静态检查和离线验证。安装脚本**不会 start/enable
服务、建立生产隧道、改变美国服务、改变 CPU timer、调用 TikTok 或打开发布闸门**。
正式切线由主任务协调。

## 固定合同

- 业务源码：GitHub 已发布的 9425b39fa45390b3dc107f353dc6ef436415365d。
- 香港 /data 位于扩容后的根文件系统，UUID
  659e6f89-71fa-463d-842e-ccdf2c06e0fe；按 UUID＋剩余容量守卫，
  **不能使用 ConditionPathIsMountPoint=/data**。默认预留 50 GiB，禁止低于 30 GiB。
- 独立目录：源码 /data/tt-post-gpu/releases/COMMIT，current为链接；
  Python 3.10 基座 python-base、venv runtime、二进制 ffmpeg、
  配置 config、日志 logs、缓存 cache、临时目录 tmp 均在该树内。
- 状态：/data/tt-post-publisher 和其 direct-outro-work 子树独立，
  不改变 job_id、profile、caption、发布时间、COS URL和资源指纹。
- TT_POST_LIVE_ENABLED=0、TT_POST_MANUAL_CANARY_ENABLED=0为强制隔离值。
  公网拉流继续使用原 COS，不切换为本地文件托管。
- 原包要求 Python >=3.10；复制美国精简 Python 3.10 基座并建独立 venv，
  不降级 Requests，不更新系统包或其他业务环境。
- 美国约5GB历史 jobs/ttpreview-* 保留原地并记录路径、大小、mtime和SHA。
  不导入香港在线 jobs、不删除；只预拷稳定资源、manifest、ledger和必要artifact。

### 已批准的 FFmpeg 运行时兼容适配

美国 FFmpeg N-124254-g397c7c7524-20260429 要求 NVENC API 13.0／驱动至少570，
香港565.57.01只支持API12.2，第一次隔离合成编码因此拒绝启动编码器。
未升级共享驱动，也未改动FB、Drama或其他业务环境。

经主任务批准，香港使用 /usr/bin/ffmpeg 与 ffprobe 的私有副本，版本
n7.1-152-gd72536008a-20250113，仍放在 /data/tt-post-gpu/ffmpeg/。
业务源码9425、媒体profile、素材规则、COS URL和已准备媒体均不改变。

| 私有二进制 | SHA256 |
| --- | --- |
| ffmpeg | c34815e5271aecd549e2334a659eebee62de5c86f763d1f15026b11582f1184d |
| ffprobe | bf7b813bb81f01695a38841e697d6fd858c194baf13017e78c2855af502e644a |

美国原始二进制保留在 /data/migrations/RUN_ID/tt/us-ffmpeg-before-compatibility，
归档仍保留在同批 direct-inputs/ffmpeg.tar.gz；版本与校验结果分别记录于
tt/ffmpeg-installed.json、tt/ffmpeg-compatibility.json。失败验证证据不覆盖。
兼容副本已通过2秒真实 HEVC_NVENC 合成：720×1280／30fps／hvc1／AAC48k双声道；
源版本73项Fake回归重新通过。正式切线仍要求两lane完整本地渲染和媒体契约通过。

### Direct Outro 30fps 适配与失败证据

完整离线验证发现：固定9425代码的 direct_outro 拼接图在上述FFmpeg7.1上，
只有 -fps_mode cfr、没有显式输出帧率时，实际得到25fps。12秒合成源自身为30fps，
有效源长7.666667秒，大于源码最低1秒；成片HEVC/hvc1、尺寸及音频均合格，
失败仅在原验证器强制30±0.01fps。不能靠放宽profile或延长样本绕过。
证据保留在 validation/direct-outro-probe-02，包含每条命令、完整ffprobe、
清理前成片stage4/5及failure.json；无真实上传、TikTok调用或发布账本。

经协调者批准的最小适配是 ffmpeg_adapter.py：私有原二进制保留为ffmpeg.bin，
适配器只接受9425的direct-outro单输出布局、完整滤镜图、编码参数及输入输出目录，
在末尾输出文件前补 -r 30。静音源和正常音轨均受同一验证，
random_overlay、独立normalize、版本查询和其他调用保留原参数向量直接exec；
模糊direct参数、额外输出、已有-r或变化的图/编码配置会明确拒绝。
该适配不改业务源码、profile、既有ready媒体、全局FFmpeg或其他业务目录。

仅从已发布并核对的运维提交执行：

    bash install-ffmpeg-adapter.sh OPS_SHA RUN_ID

安装要求4个目标生产unit均停止，核对私有原binary SHA，先保留备份和版本证据，
不启动服务。再经协调者授权，用相同PrivateNetwork静态模板和新的instance执行一次
direct-only离线准备＋reuse；仍由9425原validate_prepared_output验收。
units/tt-gpu-offline-direct-outro.conf仅可安装为该离线instance的drop-in，不能用于生产。
失败不得切线。当前安装脚本和参数测试不等于实际媒体验证通过。

## 执行顺序

1. 协调者验证 GitHub 业务源码和本目录运维提交，预复制固定资源及代码，
   建立私有运行时；美国env/secrets通过受信SSH在内存中中转，不能进入Git。
   源配置落香港 /data/migrations/RUN_ID/tt/source-config/。
2. 使用精确运维提交执行 bash install-isolated.sh OPS_SHA RUN_ID。
   备份配置/unit，生成关闭闸门的两lane配置，安装unit并验证，保持未启动。
   重复执行遇已有checkpoint会拒绝覆盖，应先核查。
3. 在数据目录设置 TMPDIR、PIP_CACHE_DIR和PYTHONDONTWRITEBYTECODE=1，
   运行源版本 scripts/test_tt_gpu_worker.py（FakeTikTokAPI/FakeObjectStore）。
   再运行 verify_offline.py --output-root /data/tt-post-gpu/validation/UNIQUE_ID。
   离线脚本用本地合成视频、真实FFmpeg、假下载/假存储/假TikTok，并禁止Python网络连接。
4. 主任务批准后才可启动目标关闭闸门的服务，GET /health验收。
   不做真实prepare/publish/canary/reconcile，不建生产隧道。

完整验证使用独立静态模板 units/tt-gpu-offline-validation@.service。
协调者从已发布运维提交安装模板并daemon-reload，核对资源与关闭闸门后才启动
tt-gpu-offline-validation@UNIQUE_ID.service。模板无Install/timer，
不会运行正式worker入口；PrivateNetwork隔离网络，采用生产unit相同权限限制，
并额外将生产state和config设为只读，写入范围只有validation、缓存、临时目录和日志。
每次使用新UNIQUE_ID保留失败证据。不要以systemd-run省略不支持的安全属性来代替：
香港239客户端不支持部分transient属性，但静态unit支持。

香港运行上述Python运维脚本请使用 /data/tt-post-gpu/runtime/bin/python；
美国snapshot请用 /root/miniconda3/envs/drama-voice/bin/python。
两台GPU主机的默认python3仍可能是3.6，不能运行带future annotations的运维脚本。

## 切线与账本

CPU入口门禁、7个timer/path暂停、在途排空、美国两业务及两隧道停用/屏蔽、
最终增量复制和目标发布闸门均由主任务掌控。旧GPU没有优雅SIGTERM排空；
必须先确认CPU在途claim为0、GPU无工作线程/子进程/连接/锁。

tt_migration.py snapshot --root STATE_ROOT --output REPORT --require-idle
生成两lane每个JSON的SHA、计数和风险状态。只在源已冻结后用fingerprint做最终一致性验收；
预拷结果不代表最终同步。init_rejected等失败账本必须保留，不能删后重新init。
未来ready任务可原样迁移，不能提前发帖或修改时间。

### CPU 排空与一致性备份入口

python3 cpu_state.py snapshot 只读输出两库任务状态、claim存在/有效/过期、
unknown_outcome、publish_id数量、执行服务和7个触发器、TT HTTP连接、源码版本。
不读取Token值，不调用HTTP或导入线上服务。状态查询有5秒期限，指纹查询上限20000行。
未来无claim的ready允许保留；过期claim和未知发布结果仍阻止切线。

主任务已关闭公网写入口并暂停7个触发器后，执行：

    python3 cpu_state.py snapshot --samples 3 --require-drained --require-paused

连续三次均排空、触发器暂停且发布事实指纹稳定才退出0。它不代替公网门禁证明；
查询期间即使无请求，也不能视为已关闭入口。此脚本从不stop任何unit。

最终备份由协调者执行：

    python3 cpu_state.py backup --run-id RUN_ID --ingress-gate-confirmed

该动作验证CPU数据盘UUID并再次检查排空，然后用SQLite backup API从mode=ro连接
保存两库到 /mnt/data-disk/migrations/RUN_ID/tt/cpu-sqlite-final，权限0600。
不用cp主库或拼接WAL，备份后quick_check并记录文件SHA、前后发布事实指纹。
TT指标任务可继续写与发布无关的指标表；两库分别一致，发布事实必须前后完全不变。
已有备份目录不会覆盖；失败保留partial/检查证据，需要协调者检查后决定下一步。

### GPU 最终同步：精确集合，不覆盖式合并

下面仅供主任务在正式维护窗口批准后执行。不能用预拷fingerprint作为最终证明。
保持CPU公网TT写入口关闭、7个timer/path暂停，等待所有自然runner退出；
ready未来任务允许保留，claim（含过期）、未知发布结果、GPU线程/子进程/HTTP均须排空。
CPU的SQLite仍留CPU，不搬到GPU；TT GPU持久状态是两lane独立JSON manifest/ledger。

1. 用上述cpu_state.py backup得到CPU
   /mnt/data-disk/migrations/RUN_ID/tt/cpu-sqlite-final/manifest.json，
   计算其CPU_MANIFEST_SHA；仅将该不含Token的元数据文件安全复制到美国
   /data/migrations/RUN_ID/tt/cpu-backup-manifest.json，SQLite本身不外传。
2. 协调者运行control/source_fence.py tt，持久mask美国2worker+2tunnel。
   确认4unit inactive且MainPID=0；保持CPU维护门禁。记录该次5分钟内的新drain checkpoint。
3. 在美国从已发布checkout执行，不能使用默认python3.6：

       /root/miniconda3/envs/drama-voice/bin/python TT_CHECKOUT/final_state.py export \
         --run-id RUN_ID --cpu-backup-manifest /data/migrations/RUN_ID/tt/cpu-backup-manifest.json \
         --cpu-backup-manifest-sha256 CPU_MANIFEST_SHA --coordinator-checkpoint FRESH_TT_CHECKPOINT

   脚本独立复核美国主机/数据盘/4unit持久mask、9425源码、CPU backup成功/quick_check，
   再导出 /data/migrations/RUN_ID/tt/final-source/tt-final-state.tar.gz。
   export-receipt.json输出archive、bytes、sha256、state_index_sha256、state_fingerprint、
   file_count、cpu_backup_manifest_sha256、coordinator_checkpoint_sha256和source_fenced。
   state-index.json绑定4个状态目录每个文件的SHA、全部素材/字体指纹、3个源env/secrets的SHA，
   以及CPU备份元数据和源FFmpeg来源。归档不含env内容、Token、缓存或历史preview。
4. 用本run受限通道复制该归档，按receipt核对size/SHA，再从香港已发布checkout执行：

       /data/tt-post-gpu/runtime/bin/python TT_CHECKOUT/final_state.py import \
         --run-id RUN_ID --archive RECEIVED_ARCHIVE --archive-sha256 EXPORT_ARCHIVE_SHA \
         --cpu-backup-manifest-sha256 CPU_MANIFEST_SHA --state-fingerprint EXPORT_STATE_FINGERPRINT

   新端4unit必须停止、LIVE/MANUAL均关闭；先验证归档成员集合、每文件SHA、CPU备份关联，
   再核对源资产/config与预拷一致。若漂移即停下，不自动改素材、配置或冻结profile。
   经验证的4个目录分别rename换入，整个旧目录移至tt/final-import-before/state，
   因而precopy已删/已替换的旧JSON不会残留。任何中途失败保持全端隔离，保留partial供检查。
   tt/final-target-receipt.json必须ok、state_file_set_exact、assets_exact均true，
   fingerprint及CPU_MANIFEST_SHA与美国/CPU对应值一致；其中还记录目标adapter/bin/probe SHA。

### 目标隧道、原开关与正式放行

units中新增2条隧道复用 /etc/x-post-media-repair-tunnel 的既有专用key/known_hosts，
不复制个人私钥、不改authorized_keys。CPU18830→HK8830，CPU18834→HK8832，
固定root@43.166.187.96、StrictHostKeyChecking、ExitOnForwardFailure及keepalive。
从核对的checkout执行 bash install-tunnels.sh OPS_SHA RUN_ID，仅安装与daemon-reload，
不enable/start。该脚本保留target-tunnels-before，安装gate_handoff.py供后续明确批准。

source-live-gates.json记录源两个实际PID的4个布尔及env比对，只含布尔/hash。
本次已捕获两lane LIVE=true、MANUAL_CANARY=false、AUDIT=true、URL_PROPERTY=true。
最终source config hash必须仍与该证据一致，否则重新只读调查，不能默认改为全1。
已确认最终同步、两lane离线完整通过、旧源fence和CPU门禁后，由主任务执行：

    /data/tt-post-gpu/runtime/bin/python TT_CHECKOUT/gate_handoff.py \
      --run-id RUN_ID --source-gates-sha256 SOURCE_GATE_PROOF_SHA \
      --final-fingerprint EXPORT_STATE_FINGERPRINT \
      --coordinator-confirms-source-fenced --coordinator-confirms-cpu-drained \
      --coordinator-confirms-ingress-gated --coordinator-confirms-offline-verified

此入口只恢复每lane被捕获的4个布尔，保留MANUAL原值和所有路径/profile/COS配置，
先备份到target-gates-before；不启动任何服务。4个coordinator参数是主任务明确确认，
不是脚本已独立检查外部状态。脚本独立检查目标停止、源配置/凭据/证据SHA及最终JSON指纹。

放行顺序由主任务逐项执行：先启动两个worker，GET本地8830/8832 health核对lane/profile，
再显式启动各自tunnel，CPU核对18830/18834监听及GET health，最后恢复原触发器/写入口。
禁止用POST或人工提前发布作测试。Requires+After不会保证worker显式restart后tunnel自动恢复；
每次维护需记录原tunnel active状态，worker起来后**显式start原active的对应tunnel**并复核CPU端口。
不能把worker active作为端到端恢复证明。enable/start恢复的是批准的原状态，不盲目全开。

## 回滚

协调者先关闭CPU写入口和触发器，等待自然在途排空，然后先停香港两条tunnel，
确认HTTP/线程/子进程/claim/未知结果均已排空后才停worker。紧急直接断隧道不会终止
GPU线程，可能造成CPU看到失败而GPU仍在发布，不能以连接消失判定安全。
保留香港新增/更新的manifest、所有publish ledger（含failed/init_rejected）、publish_id、
unknown事实、必要媒体及最新CPU SQLite快照，完整回传/核对后再考虑恢复旧端入口。
未知发布结果尚未解决时不能恢复旧端重试。CPU SQLite保持最新；
禁止恢复旧数据库覆盖新发布。仅配置需要撤回时执行
bash rollback-target.sh RUN_ID：要求目标2worker+2tunnel全部inactive，仅恢复配置和unit，
不恢复账本、不删除媒体、不启动任一端。源码、环境、媒体均留存用于审计。

## 本地检查

### 私有 Python CA 路径修正（2026-08-28）

香港迁入 Python 的 OpenSSL 默认 CA 仍指向美国绝对路径，导致 Creator Info TLS 失败。
仅复用美国原 CA：226168 bytes、145 个证书、SHA256
`b6e66569cc3d438dd5abe514d0df50005d570bfc96c14dca8f768d020cb96171`。
不修改业务9425、FFmpeg、manifest、发布门闩或三个现行env文件；不关闭TLS校验。

协调者先关闭CPU TT写入口/7触发器并自然排空，然后停香港2 tunnel和2 worker。
GitHub提交、精确部署完成后，只从对应checkout执行：

    /data/tt-post-gpu/runtime/bin/python TT_CHECKOUT/install-trust-store.py \
      --run-id gpu-service-migration-20260828T1502 --ops-commit FULL_COMMIT \
      --ca-file /data/migrations/gpu-service-migration-20260828T1502/tt/source-trust-ca-bundle.pem

安装器硬校验HK hostname、数据UUID/容量及四unit停止，拒绝任何env内SSL_CERT_*覆盖。
原文件私有备份到tt/target-trust-before；原子安装trust/ca-bundle.pem、空trust/certs、
verify_trust.py和两worker的40-tt-private-trust.conf；daemon-reload但不enable/start。
ExecStartPre校验无symlink、精确CA SHA、正确环境，以及默认SSL context仍为
CERT_REQUIRED/check_hostname=True。校验无网络调用。协调者须另作不带Token的TLS探测，
再启动worker并显式启动原active tunnel、检查CPU端口后恢复原触发器及写入口。
回滚同样须先停四unit，按target-trust-before/manifest.json逐项恢复原文件或撤下原不存在的
drop-in后daemon-reload；原env和业务账本保持不变。旧CA配置本来不可用，不能仅撤回修正就放行业务。

python -m unittest discover -s ops/gpu-service-migration-20260828/tt -p 'test_*.py' -v
以及 Python 语法检查、shell语法检查、git diff --check。
