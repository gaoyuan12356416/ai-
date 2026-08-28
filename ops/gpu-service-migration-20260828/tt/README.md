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

## 回滚

协调者先停止并排空香港，保留其新增发布事实，将新增/更新的manifest、
publish ledger及必要媒体回传美国，再考虑恢复旧端入口。CPU SQLite保持最新；
禁止恢复旧数据库覆盖新发布。仅配置需要撤回时执行
bash rollback-target.sh RUN_ID：要求目标服务inactive，仅恢复配置和unit，
不恢复账本、不删除媒体、不启动任一端。源码、环境、媒体均留存用于审计。

## 本地检查

python -m unittest discover -s ops/gpu-service-migration-20260828/tt -p 'test_*.py' -v
以及 Python 语法检查、shell语法检查、git diff --check。
