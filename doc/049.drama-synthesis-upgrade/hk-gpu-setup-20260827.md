# 香港 GPU 独立搭建与隔离验收（2026-08-27）

## 授权与分阶段边界

用户已授权代为通过 SSH 补齐香港 GPU 运行环境、去背景音乐脚本/模型、随机模板素材、CPU 受控隧道、新服务配置与真实媒体样本验证。

- 目标：`43.154.250.89:/data/drama-synthesis-gpu`，新服务监听 `127.0.0.1:8787`，CPU 新隧道为 `127.0.0.1:18788`。
- 环境搭建阶段 CPU 正式制作地址保持旧 `18787`，不提前切流或重启主 API/job worker。
- 用户随后明确授权“完成环境搭建后继续完成部署任务，并用【Shahrul Ikmal】频道进行测试”。环境与媒体门禁通过后，继续 CPU 发布、数据库三表迁移、内部 unlisted 测试与一条评论；不发布 public 测试视频，不触发 X/FB/TikTok 发布，不新建收费云资源。
- 用户进一步明确：支持问题只通过 SSH 处理，禁止进入或操作腾讯云管理后台。已结束本次后台标签会话；后续不再采用云控制台路径。
- 保留原有 X worker/tunnel 与旧 GPU 多端口隧道；原有 `ads_video_producer.service` crash-loop 为预存问题，不在本次修复范围。
- HK `/data` 实际在根盘 `/dev/vda1`，不是独立数据盘；初始可用约 163 GiB。所有新增大文件限定在上述专用目录，不声称实现了独立磁盘隔离。

## 预检发现及开发责任

旧候选 `2b26b540660fd3687fa7c66e68a246d1a706136a` 的离线应用测试不等于可安装验证。新发现：

1. worker unit 使用系统 Python 3.9，HK 缺少应用依赖；去背景音乐脚本及完整依赖锁未交付。
2. 旧 GPU 的 Python 3.10 / PyTorch CUDA 13.0 环境不能未经验证移植到 HK 驱动 565.57.01。
3. `ProtectHome=yes` 下不能继续使用 `/root` 默认解释器、脚本、模型缓存路径。
4. CPU 复用 SSH key 仅允许 `18820`，须保留原约束并精确追加 `18788`。
5. 旧资产目录为 root:root 0500，目标必须给专用服务用户只读访问权限。

以上运行包缺口由开发补齐并复测，不归因为用户服务器配置错误。CPU 统一数据库采用真实三表一致性备份与本机隔离恢复演练；用户附件并未要求购买全量云集群副本。不得伪造云 API 证据或跳过数据库权限门禁。

## 实施与验收计划

1. 保存服务 PID、端口、存储及新建前状态；SSH 配置变更独立备份并检查并发漂移。
2. 固定独立 Python/runtime 路径及依赖，补齐 Demucs 脚本、离线模型配置、worker env 示例与 systemd unit。
3. 固定 FB 资产 manifest SHA `028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f`，验证 20 个资产及其逐文件 SHA/size；正式随机目录保持四层 315 种组合，不加入 light。
4. 本地评审与 focused 回归后提交 GitHub；HK 从 GitHub 获取精确 SHA，保存运行环境与模型清单。
5. worker 仅携带制作所需的 COS/worker 配置，不复制 CPU 全量 `.env`、数据库或发布凭据。
6. 验证匿名健康、认证边界、catalog；用独立 canary job ID 测试 concat、no-BGM、cover-intro、random 的真实 FFmpeg/CUDA 路径及 COS 输出，不创建正式 CPU 任务。
7. 验证去背景音乐音视频时长/codec/可解码、随机成片 recipe/hash/规格、失败路径与幂等；复查旧端口及 X PID 未被改变。

## 当前状态（2026-08-27 12:38，北京时间）

香港 GPU 独立环境、真实双模式合成与重启重放已通过；整体正式发布仍 HOLD，阻塞在生产数据库合法授权。汇总见 [部署状态](deployment-status-20260827.md)。

- HK 当前 dark release：`e1f5a1d04cfb510df9c2444ac592adec2827508b`，已 GitHub push/readback，HK 独立 releases 目录 clone、detached HEAD 与 clean tree 校验完成。上一隔离版本为 `c719bebf72be900ec3853858dc53b36b83beffd2`；CPU 候选和三表演练继续绑定 c719beb，正式制作地址仍为旧 `18787`。
- Python `3.10.20`、torch/torchaudio `2.5.1+cu124`；55 项完整依赖锁逐项一致，`pip check` PASS。专用用户真实 CUDA tensor 运算 PASS。
- 四个 `mdx_extra_q` checkpoint 已下载并逐一核对完整 SHA；包内包含同版 YAML。专用用户真实 CUDA 四模型推理通过：1 秒静音输出 44100 帧、峰值 0；2 秒反相立体声输出 88200 帧、峰值 0.0079345703125，均为有限值。
- 独立 QA：六套合并共 188 项，首次 187 PASS、1 项迁移文档文本合同失败；补回真实错误说明后仅该项定向复测 PASS，没有重复整套，不称一次全绿。27 文件语法及 Python 3.9 AST、diff PASS；5 个增量代码文件测试前后 SHA 一致。此统计包含 c719beb 的 166 例，不叠加；另 5 项内存 mock 媒体对抗也不计入。实际 HTTP 验收另列下文。
- 新版本通过与 worker 一致的 systemd 沙箱 `--check-app-import` 预检；早期临时预检 unit 遗漏正式 unit 的 ReadWritePaths，被目录可写门禁正确拒绝，补齐相同声明后通过，未放宽权限。最终真实重启只涉及新增 worker/tunnel，12:37:56 回读 PID 为 1095021/1095027，NRestarts=0；旧 X worker/tunnel 91290/91292 均未变。
- CPU 仅追加原 HK 隧道 key 的 `permitlisten="127.0.0.1:18788"`，保留 `18820`、from/restrict/forced-command；`sshd -t`、精确一行差异及原 PID/旧端口检查 PASS，无 SSH 重启。
- CPU SSH 备份：`/mnt/data-disk/drama-synthesis-hk-setup/backups/20260827T110405+0800`，备份 SHA `cf869d27b397dda3ea261edff3fc9d54ea110c8980272d83c1556d742cd7af0b`（已通过 SSH 对实际备份再次读取）；新文件 SHA `3671dba18796b23b0cb85f8dd5566ea78d455c690228a9dc585fa948eb5b4b6c`。
- 资产只读归档 SHA：`84c37899fd37fa5590f19f4f21a56488837aa017fab6cfa0958fa918c58f31a0`，520345600 bytes。首次 COS multipart 失败后，以同一 SHA 和已验证 ETag 断点续传完成；私有对象匿名 HEAD=403。HK `/data/drama-synthesis-gpu/assets/fb-v3-028326ab2114` 的 20 个文件 SHA/size 全部匹配，文件 0444、目录 0755；认证 catalog 返回 3/5/3/7 四类，无 light。
- 目标频道只读唯一定位：app `1479`、local channel `263`、account `255`、YouTube channel `UCHJ1jFaYuW8g5EM7hM5pPpg`。真实测试前仍需刷新及 `mine=true` 核验身份；当前没有上传或评论。
- 当前 HK `.env` 仍限定 `COS_PREFIX=drama-synthesis-canary/20260827`，对应 `DRAMA_PUBLIC_BASE_URL` 为隔离 COS 前缀；live/sync 均为 0，文件 root:root/0600、SHA `0fe4d30f3b1f154e4391bbad8df2c2a213aae6a4e44a5605dd8b8f5cf5c40106`。正式切流前须备份配置、改正式前缀并重新验证，不把隔离样本目录当成生产激活完成。

## 历史真机缺陷（已由 e1f5a1d 修复复验）

- v1 合成样本暴露两个缺陷：随机图层的自动滤镜线程池超出新服务 TasksMax=128；横向片头切到竖向剧集时 `setpts=PTS-STARTPTS` 重置导致丢掉片头。原 FB 函数保持不变，仅 drama 调用层固定 `-filter_complex_threads 2`，对唯一源视频前缀改为 `setpts=PTS`，并增加固定 0.15 秒的源/容器/视频流时长一致性门禁。
- 相同 cgroup 的 A/B 验证：修复前产物 3.966667 秒，修复后视频流 5.0 秒；长片少 1 秒、音频补齐掩盖截断、NaN 时长均拒绝随机产物上传。
- c719beb 的 HTTP v2 auto job `0a7cd9dc5cc4a55e1e9b89c76b40c74e` 已实际完成下载、片头、拼接、CUDA 去背景音乐、随机模板和 COS 三个合成素材文件；下载校验、5 秒时长、随机 720×1280/High/150 帧及全帧解码通过。合成素材位于独立 `drama-synthesis-canary/20260827`，不是 YouTube 发布。
- 当时同一 job/payload 的第二次 POST 幂等断言失败，并重新开始下载/制作；v2 manual 分支未执行，v2 整套为 FAIL。旧 manifest 与样本保留；不再把被覆盖前的 v2 SHA 当成当前 COS 对象证据。根因及修复见 [BUG-020](bugs/BUG-020.md)；以下 v3 使用全新固定 job。

## 最终真实媒体验收（PASS）

e1f5a1d 的 `drama-synthesis-gpu-acceptance-v3-20260827.service` 完成 fresh 验收，报告 elapsed_seconds=79.44；随后真实重启新增 worker/tunnel，再执行 `drama-synthesis-gpu-replay-v3-20260827.service`，运行 1.710 秒。两个临时 unit 均 Result=success、MainPID=0。时长和服务结果来自主流程 SSH 回读，不是独立 QA 再跑一次。

- auto job `309b8450f03fd01de853cf4fa8b184ed`：concat、no-BGM、random 三输出；manual job `8474911734767ff621d9ddcdd7363565`：冻结四层各一个子模板、以 no-BGM 为源的 random 输出。
- 实际执行下载、1 秒横向封面片头、两段竖向素材拼接、四模型 CUDA 分离、NVENC 渲染及隔离 COS 上传；共下载核验 4 个产物，H.264/AAC，两个随机产物均 720×1280 High、5.000000 秒、150 帧，全帧解码通过。
- 健康、未授权 401、非法 job 400、认证目录 315 组合通过；manual 渲染繁忙时新渲染 503，健康/目录仍可访问，封面回调 200。
- 即时重复相同 POST 和服务重启后的相同 POST 均复用已完成产物，业务字段、manifest SHA/mtime 不变，workdir 未重建。重启重放时不启动 fixture HTTP 服务，证明不依赖原进程缓存或重新下载/渲染。
- 每个随机视频抽取 0.5/1.1/3.1/4.8 秒，共 8 张 PNG；独立 QA 已核对报告 SHA、配方 SHA、产物映射并查看 8 帧，片头保留、两段顺序正确、末段与模板继续变化，抽样未见冻结。此为只读证据复核，非第二次真机运行。
- 报告 `/data/drama-synthesis-gpu/work/acceptance/http-media-20260827-v3.json`，SHA `40746316694eeb4d34fb4511713acb13b8de14cff382f6116fdd99f1351f2175`；本地报告及 8 帧保存在仓库未跟踪的 `output/hk-media-20260827-e1f5a1d/`，不提交媒体或生产数据。

| 产物 | 字节数 | SHA-256 |
| --- | ---: | --- |
| auto concat | 691334 | `9351adf43e7474be4e8ed00509415b2653e952355c2d6a2f46021ae1143eebb7` |
| auto no-BGM | 747591 | `53c1f51bc7e12510e80b4d42209da83c837e56cc9920f56a2936540e37c5fb37` |
| auto random | 3344876 | `797442b1f2e9be45add893206e6de5df28db559e50e21ae55abb5d1aa38eb423` |
| manual random | 3343325 | `7f37613e315bf3fce026441611d2d1e292fb345d5fb2dce8d6a3bb1a45a229c6` |

重启前后持久化 manifest 摘要一致：auto `7e2911c8887f0d19eff6a7136c7be36d4297d4b112ffa3cdcf0580e5b9f97d1c`，manual `c134ef181b280fd0e4dbbf23652713219c674daa6cdf9499f3a7b171067e184f`。真实 YouTube 发布数仍为 0。

CPU 12:37:58 最后只读回查：API/job worker PID 3841722/1212、NRestarts=0 未变；app.py SHA `a956fb9952aa09d8d911cf3a5c54b58525cb81935d92d0ede698af9c681675a3` 未变，GPU 地址仍旧 18787；SQLite quick_check=ok、20 done、无活动任务。旧 18787、新 18788、18820、CPU API 及现有 X 短链均 HTTP 200；未生成的 YouTube 数字路径 GET 404、POST 403。没有重启或修改原业务来取得测试结果。

## CPU 三表备份与隔离恢复（已通过）

- 候选固定为 `c719bebf72be900ec3853858dc53b36b83beffd2`，生产源固定只读入口 `101.32.56.53:63350/kunlunads_dev`。单个 READ ONLY consistent snapshot 导出三表；未在生产执行 DDL 或写入。
- 备份目录：`/mnt/data-disk/drama-youtube-rehearsal-20260827a11c0001/snapshot`，目录 root:root 0700，文件 0600。244151 条 videos、53 条 comments、55105 条 publish_log，共 299309 条。
- 仅恢复到 CPU loopback `127.0.0.1:23357` 的隔离 MySQL 5.7.44，独立 schema `drama_youtube_rehearsal_20260827a11c0001`、实际数据盘 bind mount。固定 image digest `mysql@sha256:dab0a802b44617303694fb17d166501de279c3031ddeb28c56ecf7fcab5ef0da`。
- 12:05:58–12:07:08（北京时间）完成 dry-run → apply → apply(no-op) → dry-run；45 个旧字段、旧索引、每行数据指纹与行数保持一致；3 个新增 external-id 列均为 NULL，3 个唯一索引验证通过。完成后停止本次新建容器（exit=0）释放资源，保留容器与备份数据，未删除或更改既有容器。
- snapshot manifest SHA：`426685eda5041d332cde8f70ca724a7bbc3ae6038a0da6d02d1fabc2233f0603`。
- rehearsal result SHA：`0178a8b633c6433cffca4be32cdb4b5adfaa47e63bcaafb1398d847455d7d43b`。
- backup evidence SHA：`36579d5ed7a2234d821638b3644c4b32ce024354cbdc136aa97b53dbc3fe9dec`。
- 这是三表级可恢复与迁移演练证据，不是 CynosDB 全集群灾备证明。证据有时间和 candidate SHA 绑定；过期或候选变化时必须重新执行相应门禁。

## 当前外部权限阻断

CPU 主应用与 worker 实际配置、已授权的数据库配置文件均指向 `ads_aius@43.166.187.96`。对目标 `kunlunads_dev` 仅有 SELECT/SHOW VIEW，无三表 INSERT/UPDATE/CREATE/ALTER 或可转授权权限。不同 schema `ads_ai` 上的 GRANT OPTION 不赋予此目标库权限；Linux root SSH 也不等于 MySQL 管理权限。

尚未获得合法管理员连接或独立 migrator/writer 凭据。生产 63353 未被当作普通查询入口探测；不得改用其他库、借用不在授权范围内的账号、放宽 RPC/文件权限或进入腾讯云后台绕过。必须由授权人员提供 CPU 上可读的管理凭据文件路径，或创建方案限定的两个最小权限账号，然后继续生产迁移、18837 writer、CPU 应用切换和指定频道测试。

此时 CPU 主 API/job worker、正式 `.env`、生产 SQLite/MySQL、正式 UI 均未切换；YouTube refresh/upload/comment 为 0。

## 回滚约束

搭建阶段仅停止/禁用新增 `drama-synthesis-gpu-worker.service` 与 `drama-synthesis-gpu-tunnel.service`，不停止原 X 或 ads 服务。CPU 主程序仍走旧 `18787` 时不需要应用切回。

e1f5a1d 切换前备份为 `/data/drama-synthesis-gpu/backups/20260827T123135+0800-pre-e1f5a1d`，保存前一个 current=c719beb 及服务状态。c719beb 的旧缓存不识别新 manifest 的精确长度合同；不能在回退后让旧二进制自动处理这些已完成 job。当前安全回滚为停止新增服务并保留 manifest/COS；如确需退二进制，先冻结任务、逐项审查，不自动重制或覆盖产物。

如需回退 CPU SSH 附加权限：先停新增隧道；核对当前 authorized_keys 仍等于本次新 SHA，备份与 before evidence 一致，再原子恢复原文件、保持 root:root/0600 并执行 `sshd -t`。有并发变化即停止自动恢复。不得直接覆盖后续用户新增 key。

新增代码、依赖、模型、资产及私有传输对象保留供审计，不自动大范围删除。

## 参考依据

- [uv Python 管理](https://docs.astral.sh/uv/guides/install-python/)：安装专用的固定 Python 版本，不替换系统解释器。
- [PyTorch 官方版本组合](https://pytorch.org/get-started/previous-versions/)：torch/torchaudio 与 CUDA 构建成对固定。
- [Demucs 官方实现](https://github.com/facebookresearch/demucs)：模型与分离接口。
