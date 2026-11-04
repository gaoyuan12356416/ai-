# 部署文档

## 变更内容

本文件是部署准备方案，尚未执行 stage、切换、暂停或 restart。最终候选 SHA 与实际验收/备份记录须在执行时补齐；当前不构成部署已完成证明。

**最新用户安排：今天完成开发后停止，明天再部署。** 以下所有正式release/venv stage、service-user验收、COS验收、切换/回滚命令均为明天的待执行方案，今天不得运行；现有实验结果不替代明天最终SHA的验收。

- GitHub：`https://github.com/gaoyuan12356416/ai-.git`；开发分支 `codex/gpu-pipeline-full-offload-20261104`。
- 香港 GPU：`43.154.250.89`；原 Git 基线 `179596a9699393cd377c0e06a0bec94e171f14cd`，**不是当前运行代码的完整身份**。
- 2026-11-04 只读核查：MainPID938584 的 cwd/argv 指向 `/data/pipeline-dispatch-runtime/fb-lease-f3bd38531f6d/node`；仍使用原179596a venv 和 staged/node.env。后续 PID/状态必须重新读取。
- 新候选采用 `/opt/pipeline-dispatch/releases/<SHA>`，每 SHA venv `/opt/pipeline-dispatch/venvs/<SHA>`，staged 配置 `/opt/pipeline-dispatch/staged/hk-gpu/<SHA>`。
- 安装私有 manifest：`/root/pipeline-node-gpu.json`。本次不输出其中凭据，不复制凭据到仓库。
- 服务 `pipeline-node-hk-gpu.service` 同时管理 prepare 和 publish 子进程；`pipeline-node-hk-gpu-tunnel.service` 及独立 `tt-gpu-publisher.service` 不属于本次切换范围。

只读观测在 2026-11-04 17:56 后曾见 node inactive/MainPID0，根代理后续又观测 active；存在外部服务控制，不能据此推断用户已暂停或获准切换。每次操作前重新核查，不依赖陈旧进程状态。

## 已确认运行版本漂移与部署阻塞

`/data/pipeline-dispatch-runtime/fb-lease-f3bd38531f6d/manifest.json` 为 `status=applied`，来源是同级 `fb-lease-f3bd38531f6d-source` 的 clean Git checkout，HEAD `f3bd38531f6da7e11fbd853444b7d12d95f50ff2`。运行 node 目录无 `.git`，不能以 `git rev-parse` 失败判断来源不可信，也不能称整树等于 f3bd385。

QA 对原179596a的1,736个tracked files逐个比较：无文件缺失，四个既有文件改变；新增Python仅job_lock.py。五文件的运行字节、manifest.after与源checkout完全相同：

| 文件 | 当前 SHA-256 | 必须保留的改动 |
| --- | --- | --- |
| `features/fb_gpu/job_lock.py` | `18151e009aa45b7607479adb79b9711b08728722910ada922fe7473ba045a0d9` | 同一冻结job跨进程排他，覆盖下载/制作/上传；锁inode保留 |
| `features/fb_gpu/prepare_worker.py` | `2424f8ced78d9b276c91a2e43b5c959d9ea0023766f699f07dcf48f665ae03c8` | 下载检查租约、保留lease lost异常、输出时长校验；CPU模板解码/filter线程约束 |
| `features/fb_gpu/render_process.py` | `cac9ecce3ef12dfa90327a7655f0445f2048ceca6f1ec5de8fb02c463231b190` | render启动前及每poll调用progress_check，租约丢失退出并回收子进程 |
| `features/pipeline_dispatch/worker.py` | `25ccd40ac0b3693a2d8bfad6d8edc7f1980ca968e4bcc16a58fbcac85353273c` | check_render不等待额外RPC，deadline120秒后终止本机计算 |
| `features/pipeline_dispatch/executors.py` | `36ef4b600521ef2f4c19c04100bd40723f1b800c1f2c7901c183bca9bb428f90` | FB job锁、execution_check生命周期、runner注入progress_check |

f3bd385源分支与179596a的全树diff包含141文件及X recovery等既有代码删除。因此必须合入上述实际部署增量并回归，不能直接把该源HEAD全树当新基线覆盖GPU分支。锁路径位于现有state/media/fb-job-locks，现行ReadWritePaths足够，无需拓宽服务写权限。

生产unit组成：base unit仍指向179596a；`30-rgba-asset-cache.conf`提供既有只读缓存路径；`99-fb-lease-receipt-20261104.conf`重置ExecStart并将WorkingDirectory指向上述runtime/node。**仅替换base unit会继续启动旧overlay。**切换必须先备份并明确处理这个exact 99 drop-in，同时保留30缓存drop-in；不能批量删除整个service.d。

现有`activate_pipeline_gpu_offload.py`不支持该overlay：真实worker hash尚未纳入白名单、当前runtime argv与旧manifest release不符，且helper不备份/撤除99 drop-in。不能只加白名单后绕过runtime检查。`pipeline_node_install.activate`的is-active返回码也不足以证明inactive（deactivating等也会返回非零）；必须另行严格检查实际state、PID、cgroup和pending job。完成受测的overlay→clean release路径前，部署切换保持阻塞。

已补专用本地脚本`scripts/switch_gpu_overlay_release.py`，只适用本文已核实的hk-gpu/f3bd385运行态，不改通用安装器。`plan`只读；`apply`与`rollback`强制显式`--user-paused-confirmed`，再检查inactive/dead、MainPID/ControlPID0、递归cgroup无进程、该unit没有pending/running systemd job。这里不要求清空业务待办队列。脚本没有stop/restart/drain命令，只在上述条件已满足时reload并start本节点。

脚本核对原GitHub来源/clean commit、全部baseline tracked files和五文件准确hash、base/30cache/exact99配置及候选clean SHA/.verified/生成unit；在state/maintenance内私有备份原manifest/env/unit/drop-ins/runtime证据。切换仅替换base unit/安装manifest及撤下99，保留30。回滚检查磁盘内容和备份指纹，恢复原overlay；若在撤99后、reload前中断，允许systemd缓存处于exact前态或后态，恢复并reload后严格核对。不会自动停止已经启动的服务来回滚。

本地专用脚本19项测试通过（来源漂移、服务未停稳、cgroup残留、逐步写入/reload/start中断恢复、备份破坏/外部配置变化拒绝）；**没有在生产执行plan/apply/rollback**。明天先重新核实运行态未变，再作只读plan和根代理复审，不绕过检查。

## 配置项

私有新 manifest 从现有安装配置派生，并与实际unit/drop-ins、runtime manifest和安全环境项交叉核对；旧`/root/pipeline-node-gpu.json`不能代表当前代码身份。只更换精确候选 SHA、增加 `"gpu_native": true` 及显式环境项。节点身份、catalog、容量、凭据、工作目录保持各自现有权威来源。

| 配置 | 目的 |
| --- | --- |
| 顶层 `gpu_native: true` | 仅 GPU 节点可选；主 venv 安装 `deploy/pipeline-gpu.requirements.txt`，包含基础依赖 |
| `RANDOM_GPU_NATIVE=1` | 使用经过验收的 native 制作路径；实际适用 profile 由实现控制 |
| `RANDOM_GPU_COMPUTE=1` | 文件 SHA-256 使用 GPU；小型控制/身份 hash 仍为主机计算 |
| `RANDOM_GPU_PYTHON=/opt/pipeline-dispatch/venvs/<SHA>/bin/python` | native 子进程显式使用该 SHA 的同一隔离 venv |

这些值经 manifest.environment 显式注入；安装器会为 gpu_native 默认设置。新环境文件一旦生成即不可变，改配置应使用新的候选 SHA/staged 版本，不能覆盖旧 node.env。CPU backend 不可开启 gpu_native。

GPU pins：CuPy13.6.0、NumPy2.2.6、PyNvVideoCodec2.2.3、PyAV16.0.1、CUDA runtime12.6.77、NVRTC12.6.85、fastrlock0.8.4，含现有基础 requirements。torch/torch-aac 保持实验用途，不进入生产依赖。旧 evaluation venv 的默认 PyNvVideoCodec 实际仍是1.0.2，视频实验通过专门2.2.3路径；不得直接复用它充当生产环境。

requirements fingerprint 包含基础和 GPU 文件，旧 base-only venv 不会被静默追加依赖。新 SHA 建干净环境，主 prepare 进程也具备 GPU hash 依赖，不仅子进程具备。

## 数据库变更

无。不得为部署清空 claim、lease、receipt outbox、unknown outcome、任务代际或上传账本。回滚也不恢复旧数据库快照。

## 部署步骤

1. 合入并独立复核已部署的五文件FB租约修复，保留native watchdog/进程组回收、GPU hash、native render计时等本次改动；跑lease fence、排他锁、native取消及receipt回归。完成相关代码审查与离线测试，提交精确 GitHub SHA。只从 GitHub fetch 该 SHA 建立 clean release；不得将实验目录/scp 源码副本称为正式发布。
2. 构造 mode0600 新 manifest，例如 `/root/pipeline-node-gpu-<SHA>.json`；验证其节点/state/root/tunnel 与当前一致。候选 manifest 只保存在服务器私有位置。
3. 在仍可工作的当前节点旁边 stage 新 SHA 的 release/venv，不启动服务。先用候选安装器 `--plan` 核对路径，然后 `--verify-local` 完成本机 encode、catalog、service-user GPU runtime doctor。doctor 真实检查标准 SHA、Annex B、codec runtime 和 AAC 创建；不领任务、不调用社媒、不上传 COS。
4. 新环境再执行原有 `--verify` 完成 COS 诊断（唯一临时私有对象的读写/清理，非社媒发布），产出 `.verified`。`--verify-local` 只产出 `.verified-local`，不能代替完整激活前验收。
5. 完成真实配方整片质量、时序、CPU/RSS/VRAM、取消/恢复测试后，准备具体部署结果给用户。**如需暂停制作，先告知，由用户暂停并确认。代理不得先 drain/restart/switch 再补问。**
6. 用户可只暂停香港节点制作池（prepare target/ceiling0），无需暂停全站发布；确认该节点 prepare running0。由于现 unit 共管 publish，重启会重建该节点 publish 子进程，必须等其当前发布安全结束并保留既有回执；不改全局发布容量、不停独立发布服务或 CPU 节点。
7. 用户确认暂停后，重新取即时状态、备份，再使用已完成overlay兼容及测试的激活路径。明确99 drop-in的撤下/替换操作，切换后必须核对实际ExecStart/cwd/解释器，而非仅检查base unit。不得根据早先一次inactive/GPU0%直接切换，也不得直接使用未经修正的旧helper/installer套用旧manifest。

以下仅为执行模板，`RELEASE_SHA`/`CANDIDATE`/`BACKUP` 必须替换为已核验实际值，不能照抄占位值执行：

```sh
RELEASE_SHA='<40位已推送并验收SHA>'
CANDIDATE="/root/pipeline-node-gpu-${RELEASE_SHA}.json"
RELEASE="/opt/pipeline-dispatch/releases/${RELEASE_SHA}"
PYTHON=/opt/pipeline-dispatch/runtime/python/bin/python3.10

git --git-dir=/opt/pipeline-dispatch/repository.git fetch --depth=1 origin "$RELEASE_SHA"
git --git-dir=/opt/pipeline-dispatch/repository.git worktree add --detach "$RELEASE" "$RELEASE_SHA"
git -C "$RELEASE" rev-parse HEAD
git -C "$RELEASE" status --porcelain
"$PYTHON" "$RELEASE/scripts/pipeline_node_install.py" --manifest "$CANDIDATE" --plan
"$PYTHON" "$RELEASE/scripts/pipeline_node_install.py" --manifest "$CANDIDATE" --verify-local
"$PYTHON" "$RELEASE/scripts/pipeline_node_install.py" --manifest "$CANDIDATE" --verify
```

stage 操作需把新 SHA 的源码先从 GitHub checkout 才能使用新安装器；旧 baseline 安装器不认识 gpu_native。正式 stage 由本任务根代理在批准范围内执行，QA 只写方案。

用户确认后、切换前的私有备份模板：

```sh
BACKUP="/data/pipeline-dispatch/maintenance/gpu-full-offload-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$BACKUP"
install -m 0600 /root/pipeline-node-gpu.json "$BACKUP/install-manifest.before.json"
install -m 0600 /etc/systemd/system/pipeline-node-hk-gpu.service "$BACKUP/node.service.before"
install -m 0600 /opt/pipeline-dispatch/staged/hk-gpu/179596a9699393cd377c0e06a0bec94e171f14cd/node.env "$BACKUP/node.env.before"
install -m 0600 /etc/systemd/system/pipeline-node-hk-gpu.service.d/30-rgba-asset-cache.conf "$BACKUP/30-rgba-asset-cache.conf.before"
install -m 0600 /etc/systemd/system/pipeline-node-hk-gpu.service.d/99-fb-lease-receipt-20261104.conf "$BACKUP/99-fb-lease-receipt-20261104.conf.before"
install -m 0600 /data/pipeline-dispatch-runtime/fb-lease-f3bd38531f6d/manifest.json "$BACKUP/runtime-manifest.before.json"
```

备份还须记录当时的FragmentPath/DropInPaths、实际cwd/argv、runtime五文件hash、source HEAD与clean状态；完整保留原runtime/source/venv/env，不以旧manifest覆盖这份事实。若任一项与本审计不同，停止套用此模板并重新核对。

原active helper只作为设计参考，当前不可直接`--apply`。本次使用专用脚本验证真实runtime来源/文件hash并备份exact drop-in，不依赖通用helper的drain白名单。用户确认暂停、外部操作已使node安全停止后，脚本才可切换；不重新enroll，不修改独立发布或隧道配置。

明天最终SHA验收通过后的执行模板（不是今天的授权）：

```sh
"$PYTHON" "$RELEASE/scripts/switch_gpu_overlay_release.py" plan \
  --current /root/pipeline-node-gpu.json --candidate "$CANDIDATE"
# 先取得用户已暂停确认，并独立确认本节点已安全停止；脚本不会停止它。
"$PYTHON" "$RELEASE/scripts/switch_gpu_overlay_release.py" apply \
  --current /root/pipeline-node-gpu.json --candidate "$CANDIDATE" \
  --backup "$BACKUP" --user-paused-confirmed
```

专用脚本要求`BACKUP`尚不存在且父目录恰为`/data/pipeline-dispatch/maintenance`、名字以`gpu-full-offload-`开头，由脚本创建并保存完整快照。前面手工备份模板若已执行，必须另用一个未创建的目录作为脚本backup，不能复用覆盖。

## 验证步骤

- 空闲判断是 authority 当前 `hk-gpu` prepare running0 + 用户暂停确认 + 对应进程/cgroup 状态一致；GPU利用率0或无 compute app不是充分证据。若 unit active，publish running/receipt情况也要即时核对，不能只看“制作空闲”。
- 读 `systemctl show pipeline-node-hk-gpu.service -p ActiveState -p MainPID -p ExecStart -p WorkingDirectory -p FragmentPath -p DropInPaths -p EnvironmentFiles -p KillMode -p TimeoutStopUSec`，连同`/proc/PID/cwd`及argv确认实际新SHA、解释器和启动时间；不读取/输出环境凭据。
- 只核对新 worker 宣告能力与实际启用 profile，不把 service active当业务验收。原生路径诊断应包含 video/audio/mux/hash 所在设备及 CPU 残留。
- 对自然完成制作任务核对冻结源、配方、输出 probe/hash/大小、claim generation/receipt；不额外发社媒测试帖，不重放 unknown outcome。
- 记录实际墙钟/CPU秒/RSS/VRAM和短期自然吞吐，区分冷启动、预热与任务并发；不能承诺未测出的 CPU下降比例。

## 回滚方案

回滚目标是**实际部署前的179596a加f3bd385五文件overlay运行态**，包括原venv/node.env/base unit、30缓存drop-in、99 lease drop-in，不能仅回退裸179596a丢失租约修复。先按同样用户暂停确认和局部空闲规则取得安全切换窗口；不停止全站发布、不恢复旧数据库。

旧active helper不能直接回滚到非Git runtime。若节点已由用户确认安全停止且inactive/MainPID0、无残留unit进程/待执行job，验证原runtime五文件仍与备份manifest一致后，可恢复备份unit/drop-ins/安装manifest，只启动本节点：

首选专用脚本，`BACKUP`使用该脚本apply返回的私有目录，不能混用手工备份目录：

```sh
"$PYTHON" "$RELEASE/scripts/switch_gpu_overlay_release.py" rollback \
  --current /root/pipeline-node-gpu.json --backup "$BACKUP" --user-paused-confirmed
```

以下为手工备份的恢复参考，只有明确使用前文手工备份文件名、复核真实文件和安全停止状态后才适用：

```sh
install -m 0644 "$BACKUP/node.service.before" /etc/systemd/system/pipeline-node-hk-gpu.service
install -m 0644 "$BACKUP/30-rgba-asset-cache.conf.before" /etc/systemd/system/pipeline-node-hk-gpu.service.d/30-rgba-asset-cache.conf
install -m 0644 "$BACKUP/99-fb-lease-receipt-20261104.conf.before" /etc/systemd/system/pipeline-node-hk-gpu.service.d/99-fb-lease-receipt-20261104.conf
install -m 0600 "$BACKUP/install-manifest.before.json" /root/pipeline-node-gpu.json
systemctl daemon-reload
systemctl start pipeline-node-hk-gpu.service
```

不要把上述 inactive 回滚当作停止服务命令。运行中需要停止时仍须先遵循用户暂停确认；不得 kill 活跃制作或发布。保留候选 release/venv和诊断证据，旧回执继续由既有 outbox处理。

## 注意事项

- 当前 GDS未具备端到端直接通路；不安装系统模块、不换驱动、不重启整机作为本次应用部署的隐含步骤。
- 完整 GPU AAC 候选尚未通过质量验收；部署范围若保留CPU AAC，需要明确记为未完成的迁移项，不以GPU DSP/ADTS已通过替代完整编解码结论。
- 安装器新增逻辑已在本地 mocked测试16/16通过；尚未在服务器执行本次stage/doctor。NVRTC固定12.6.85与早期实验12.6.77不同，干净新venv必须重新验收。
