# 香港 GPU 独立搭建与隔离验收（2026-08-27）

## 本阶段授权与边界

用户已授权代为通过 SSH 补齐香港 GPU 运行环境、去背景音乐脚本/模型、随机模板素材、CPU 受控隧道、新服务配置与真实媒体样本验证。

- 目标：`43.154.250.89:/data/drama-synthesis-gpu`，新服务监听 `127.0.0.1:8787`，CPU 新隧道为 `127.0.0.1:18788`。
- CPU 正式制作地址保持旧 `18787`；不切流、不重启主 API/job worker、不执行 SQLite/MySQL 迁移。
- 不进行 YouTube/X/FB/TikTok 发布、评论或 OAuth 刷新；不新建收费云资源。
- 保留原有 X worker/tunnel 与旧 GPU 多端口隧道；原有 `ads_video_producer.service` crash-loop 为预存问题，不在本次修复范围。
- HK `/data` 实际在根盘 `/dev/vda1`，不是独立数据盘；初始可用约 163 GiB。所有新增大文件限定在上述专用目录，不声称实现了独立磁盘隔离。

## 预检发现及开发责任

旧候选 `2b26b540660fd3687fa7c66e68a246d1a706136a` 的离线应用测试不等于可安装验证。新发现：

1. worker unit 使用系统 Python 3.9，HK 缺少应用依赖；去背景音乐脚本及完整依赖锁未交付。
2. 旧 GPU 的 Python 3.10 / PyTorch CUDA 13.0 环境不能未经验证移植到 HK 驱动 565.57.01。
3. `ProtectHome=yes` 下不能继续使用 `/root` 默认解释器、脚本、模型缓存路径。
4. CPU 复用 SSH key 仅允许 `18820`，须保留原约束并精确追加 `18788`。
5. 旧资产目录为 root:root 0500，目标必须给专用服务用户只读访问权限。

以上运行包缺口由开发补齐并复测，不归因为用户服务器配置错误。CPU 统一数据库备份/演练脚本的其他预检缺口另行处理，不在本阶段绕过。

## 实施与验收计划

1. 保存服务 PID、端口、存储及新建前状态；SSH 配置变更独立备份并检查并发漂移。
2. 固定独立 Python/runtime 路径及依赖，补齐 Demucs 脚本、离线模型配置、worker env 示例与 systemd unit。
3. 固定 FB 资产 manifest SHA `028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f`，验证 20 个资产及其逐文件 SHA/size；正式随机目录保持四层 315 种组合，不加入 light。
4. 本地评审与 focused 回归后提交 GitHub；HK 从 GitHub 获取精确 SHA，保存运行环境与模型清单。
5. worker 仅携带制作所需的 COS/worker 配置，不复制 CPU 全量 `.env`、数据库或发布凭据。
6. 验证匿名健康、认证边界、catalog；用独立 canary job ID 测试 concat、no-BGM、cover-intro、random 的真实 FFmpeg/CUDA 路径及 COS 输出，不创建正式 CPU 任务。
7. 验证去背景音乐音视频时长/codec/可解码、随机成片 recipe/hash/规格、失败路径与幂等；复查旧端口及 X PID 未被改变。

## 当前状态

进行中。安装、发布 SHA、模型/资产校验、样本结果和精确回滚步骤将在实际执行后补充；不得将本计划视为已通过。

## 参考依据

- [uv Python 管理](https://docs.astral.sh/uv/guides/install-python/)：安装专用的固定 Python 版本，不替换系统解释器。
- [PyTorch 官方版本组合](https://pytorch.org/get-started/previous-versions/)：torch/torchaudio 与 CUDA 构建成对固定。
- [Demucs 官方实现](https://github.com/facebookresearch/demucs)：模型与分离接口。
