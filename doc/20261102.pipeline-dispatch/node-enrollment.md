新服务器接入不需要修改节点白名单。控制台签发一次性 enrollment token，节点按实测能力登记；中央调度决定并发，节点仅负责执行。任何平台刷新令牌仍在 CPU 权威服务内，节点只接收单操作短期密封凭据。

1. 从已推送的同一个 GitHub commit 部署代码和独立 Python 环境。依赖为 requests、Pillow、cos-python-sdk-v5，以及 cryptography 或 pycryptodome。安装带 libx264/libvpx/AAC 的 FFmpeg/ffprobe；GPU 节点还需要既有 NVENC、OpenCL、驱动和对应 FFmpeg 构建。
2. 创建专用 `pipeline-worker` 用户，将节点数据、制作工作目录和独立 Codex CLI auth 放在数据盘 `/mnt/data-disk/pipeline-dispatch` 或 `/data/pipeline-dispatch`。仅复制已审核素材目录并核对 manifest 及每个文件 SHA256。不要复制旧任务目录、队列、发布 token、上传会话或刷新令牌。
3. 私有环境文件权限为0600。以 `deploy/pipeline-node.env.example` 为节点配置；CPU 同时使用 `deploy/pipeline-node.cpu.env.example`。示例中的 host allowlist、COS domain、manifest 和私有字段必须填入该节点的真实配置，示例空值不会通过预检。node_id 使用新的唯一名称，不用代码中的固定服务器名。
4. CPU 固定 libx264、cpu_legacy；每个解码器和输出编码器均显式4线程。CPU的新随机模板 profile 与GPU旧profile分别冻结。GPU节点导入原有已验证的每渠道配置和asset catalog，覆盖工作目录到独立节点数据目录；不覆盖旧服务env或缓存。`PIPELINE_RENDER_PROFILES` 必须列出本节点能实际执行的精确profile。
5. 通过HTTPS或已有的SSH loopback隧道访问中央18846。远程明文HTTP会被节点拒绝。将一次性token写到私有文件，通过 `PIPELINE_ENROLLMENT_TOKEN_FILE` 传入；不要把token放到命令参数或日志。

首次接入命令（环境文件的应用顺序从前到后，最后文件覆盖前面同名项）：

```sh
/opt/pipeline-dispatch/venv/bin/python /opt/pipeline-dispatch/current/scripts/pipeline_node_check.py \
  --env /etc/pipeline-node.sources.env --env /etc/pipeline-node.env \
  --encode --cos-check --enroll /etc/pipeline-node.credentials.json
```

预检会加载原生renderer及素材manifest、编码一秒本地小样本、验证GPU OpenCL（若需要），并用全新唯一私有COS诊断对象检查上传和HEAD。仅删除该次创建的诊断对象；若原有COS账号没有删除权限，报告其key供维护，上传检查仍有效。预检不会创建社媒帖子、评论或触发收费封面生成。YouTube仅检查本机CLI和独立认证是否存在；首次实际封面仍按已审批的生成流程执行。

预检应以最终运行的 `pipeline-worker` 用户执行，确保FFmpeg、素材、Codex CLI及数据目录的权限与服务实际一致。若运维以root完成首次登记，启动前将新建的节点凭据文件及本节点新数据目录交给专用用户，凭据保持0600；不要更改旧服务数据目录的属主。systemd按顺序读取 `/etc/pipeline-node.sources.env`（可选私有素材/COS配置）与 `/etc/pipeline-node.env`。Codex CLI必须安装在该用户可执行、且不受 `ProtectHome=true` 屏蔽的路径。

通过后，由运维安装 `deploy/pipeline-node.service` 并按GitHub-first流程启动。也可在首次检查命令后加 `--run` 前台启动。服务收到SIGTERM仅停止接新单，等待已接任务及回执落盘后退出。中央降低并发上限时，节点也按同样规则缩容；提高上限会增加进程。每15秒刷新资源、进程与阶段状态。

两个现有节点的配置来源（只读核对，迁移时不修改旧服务配置）：

| 渠道 | 已验证GPU来源 | 保持的GPU制作配置 |
| --- | --- | --- |
| X | `/data/x-post-media-repair/config/{cos,worker,token}.env` | FFmpeg `/data/x-post-media-repair/runtime/bin/ffmpeg`，X random H264/OpenCL原profile |
| FB | `/etc/fb-page-random-overlay-gpu.env`、`/etc/random-overlay-subtemplates/fb-20260920.env` | `tt-post-random-overlay-h264-720x1280-v3`、h264_nvenc、opencl_fused_contain_v1 |
| TT | `/data/tt-post-gpu/config/{base,secrets}.env`、`/etc/random-overlay-subtemplates/tt-20260920.env` | `tt-post-random-overlay-hevc-720x1280-v3`、hevc_nvenc、opencl_fused_contain_v1；FFmpeg `/data/tt-post-gpu/ffmpeg/ffmpeg` |

共同已核对catalog是 `/data/random-overlay-gpu/assets/catalog-b5df776a88bd`，manifest SHA256 `b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c`。目录位置可不同，内容与哈希不能变；历史任务引用的其他catalog也必须保留到终态。`RANDOM_OVERLAY_ASSET_CATALOGS` 可登记多个不可变manifest，不能把新资源伪装成旧哈希。

TT生产renderer来源为GitHub commit `80acd8b4f082e121f8332dea2a34f23d739b1bb3`，本次只读抽取的worker.py、random_overlay.py及compositor.py与该commit的git blob完全一致。当前工作树与该release的共同祖先后TTworker未另有修改，因此精确引入其random/source_direct能力，再添加显式CPU随机profile；原GPU profile、随机配方和去重规则保持原实现。没有在生产机器热补丁。

自动安装入口为 `scripts/pipeline_node_install.py --manifest /绝对路径/0600私有配置.json`。默认只 stage：从 GitHub 拉取精确40位SHA、建立该SHA独立venv、核对并复制指定catalog到新节点数据目录（或共享已有只读catalog）、生成私有env及本节点专用unit，不接任务、不启动worker。配置结构见 `deploy/pipeline-node.install.gpu.example.json`；CPU使用相同结构，backend改cpu、state_root改CPU数据盘、删除tunnel、python及FFmpeg填写本机路径，并应用CPU示例的profile、libx264/cpu_legacy与4线程配置。其他服务器只需新node_id、真实性能及工具配置。

`--plan`只输出非敏感路径；`--verify`以最终pipeline-worker用户核对catalog、实际一秒编码和COS权限；`--verify --enroll`在验证通过后登记，token仅从私有文件读取。首次登记前将token文件与独立Codex auth授权给该专用用户。GPU enrollment只启动新增专用SSH转发unit，使用已有受限key及known_hosts，原隧道不改。`--activate`需要已验证及已登记，并拒绝替换正在运行的同名worker；更新前须先中央drain，待该节点所有在途任务完成。

协调器端口为18846；旧8846属于其他既有服务，不能占用或重启。已只读验证GPU受限SSH身份允许向CPU loopback18846转发（未监听时返回connection refused，非权限拒绝）。安装器及新unit只访问18846，不修改旧端口服务。依赖锁定在 `deploy/pipeline-node.requirements.txt`；上线前仍需在两机实际运行预检，离线mock测试不等于平台发帖验收。
