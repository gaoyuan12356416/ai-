# 部署文档

## 变更内容

本文件是部署准备方案，尚未执行 stage、切换、暂停或 restart。最终候选 SHA 与实际验收/备份记录须在执行时补齐；当前不构成部署已完成证明。

- GitHub：`https://github.com/gaoyuan12356416/ai-.git`；开发分支 `codex/gpu-pipeline-full-offload-20261104`。
- 香港 GPU：`43.154.250.89`；现有基线 `179596a9699393cd377c0e06a0bec94e171f14cd`。
- 当前代码 `/opt/pipeline-dispatch/releases/<SHA>`，每 SHA venv `/opt/pipeline-dispatch/venvs/<SHA>`，staged 配置 `/opt/pipeline-dispatch/staged/hk-gpu/<SHA>`。
- 安装私有 manifest：`/root/pipeline-node-gpu.json`。本次不输出其中凭据，不复制凭据到仓库。
- 服务 `pipeline-node-hk-gpu.service` 同时管理 prepare 和 publish 子进程；`pipeline-node-hk-gpu-tunnel.service` 及独立 `tt-gpu-publisher.service` 不属于本次切换范围。

只读观测在 2026-11-04 17:56 后曾见 node inactive/MainPID0，根代理后续又观测 active；存在外部服务控制，不能据此推断用户已暂停或获准切换。每次操作前重新核查，不依赖陈旧进程状态。

## 配置项

私有新 manifest 从现有 manifest 派生，只更换精确候选 SHA、增加 `"gpu_native": true` 及显式环境项。节点身份、catalog、容量、凭据、工作目录保持各自现有权威来源。

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

1. 完成相关代码审查与离线测试，提交精确 GitHub SHA。只从 GitHub fetch 该 SHA 建立 clean release；不得将实验目录/scp 源码副本称为正式发布。
2. 构造 mode0600 新 manifest，例如 `/root/pipeline-node-gpu-<SHA>.json`；验证其节点/state/root/tunnel 与当前一致。候选 manifest 只保存在服务器私有位置。
3. 在仍可工作的当前节点旁边 stage 新 SHA 的 release/venv，不启动服务。先用候选安装器 `--plan` 核对路径，然后 `--verify-local` 完成本机 encode、catalog、service-user GPU runtime doctor。doctor 真实检查标准 SHA、Annex B、codec runtime 和 AAC 创建；不领任务、不调用社媒、不上传 COS。
4. 新环境再执行原有 `--verify` 完成 COS 诊断（唯一临时私有对象的读写/清理，非社媒发布），产出 `.verified`。`--verify-local` 只产出 `.verified-local`，不能代替完整激活前验收。
5. 完成真实配方整片质量、时序、CPU/RSS/VRAM、取消/恢复测试后，准备具体部署结果给用户。**如需暂停制作，先告知，由用户暂停并确认。代理不得先 drain/restart/switch 再补问。**
6. 用户可只暂停香港节点制作池（prepare target/ceiling0），无需暂停全站发布；确认该节点 prepare running0。由于现 unit 共管 publish，重启会重建该节点 publish 子进程，必须等其当前发布安全结束并保留既有回执；不改全局发布容量、不停独立发布服务或 CPU 节点。
7. 用户确认暂停后，重新取即时状态、备份，再选择与实际 service 状态相符的激活路径。active 节点可用审核后的 activate helper；inactive 节点用 installer 激活入口。不得根据早先一次 inactive/GPU0% 直接切换。

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
```

active 路径由 `activate_pipeline_gpu_offload.py --current /root/pipeline-node-gpu.json --candidate "$CANDIDATE"` 先只读检查；真正 `--apply` 会替换 unit/manifest、daemon-reload 并发起 restart/drain，**只能在用户暂停确认后执行**。该脚本还检查 worker.py 已审查 SHA 白名单，本次若修改 worker 必须重新审查再更新，不能绕过。

若确认是 inactive/MainPID0 且该 unit 无残留进程/待执行 systemd job，候选 `pipeline_node_install.py --manifest "$CANDIDATE" --activate` 可以启动已登记节点；需将当前私有 manifest 同步为候选，但不重新 enroll。该入口拒绝 active node；不可用它强行替代正在运行的 pool。

## 验证步骤

- 空闲判断是 authority 当前 `hk-gpu` prepare running0 + 用户暂停确认 + 对应进程/cgroup 状态一致；GPU利用率0或无 compute app不是充分证据。若 unit active，publish running/receipt情况也要即时核对，不能只看“制作空闲”。
- 读 `systemctl show pipeline-node-hk-gpu.service -p ActiveState -p MainPID -p ExecStart -p EnvironmentFiles -p KillMode -p TimeoutStopUSec`，记录实际新 SHA、解释器和启动时间；不读取/输出环境凭据。
- 只核对新 worker 宣告能力与实际启用 profile，不把 service active当业务验收。原生路径诊断应包含 video/audio/mux/hash 所在设备及 CPU 残留。
- 对自然完成制作任务核对冻结源、配方、输出 probe/hash/大小、claim generation/receipt；不额外发社媒测试帖，不重放 unknown outcome。
- 记录实际墙钟/CPU秒/RSS/VRAM和短期自然吞吐，区分冷启动、预热与任务并发；不能承诺未测出的 CPU下降比例。

## 回滚方案

回滚目标为基线 `179596a9699393cd377c0e06a0bec94e171f14cd`、原 venv/node.env/unit。先按同样用户暂停确认和局部空闲规则取得安全切换窗口；不停止全站发布、不恢复旧数据库。

active 节点且 helper 的 worker白名单/clean/verified校验均通过时：

```sh
/opt/pipeline-dispatch/runtime/python/bin/python3.10 "$RELEASE/scripts/activate_pipeline_gpu_offload.py" \
  --current /root/pipeline-node-gpu.json --candidate "$BACKUP/install-manifest.before.json" --apply
```

若节点已由用户确认安全停止且 MainPID0、无残留 unit进程，可恢复备份unit/manifest后只启动本节点：

```sh
install -m 0644 "$BACKUP/node.service.before" /etc/systemd/system/pipeline-node-hk-gpu.service
install -m 0600 "$BACKUP/install-manifest.before.json" /root/pipeline-node-gpu.json
systemctl daemon-reload
systemctl start pipeline-node-hk-gpu.service
```

不要把上述 inactive 回滚当作停止服务命令。运行中需要停止时仍须先遵循用户暂停确认；不得 kill 活跃制作或发布。保留候选 release/venv和诊断证据，旧回执继续由既有 outbox处理。

## 注意事项

- 当前 GDS未具备端到端直接通路；不安装系统模块、不换驱动、不重启整机作为本次应用部署的隐含步骤。
- 完整 GPU AAC 候选尚未通过质量验收；部署范围若保留CPU AAC，需要明确记为未完成的迁移项，不以GPU DSP/ADTS已通过替代完整编解码结论。
- 安装器新增逻辑已在本地 mocked测试16/16通过；尚未在服务器执行本次stage/doctor。NVRTC固定12.6.85与早期实验12.6.77不同，干净新venv必须重新验收。
