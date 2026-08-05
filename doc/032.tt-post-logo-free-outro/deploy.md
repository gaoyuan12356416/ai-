# 部署与回滚

## 部署顺序

1. 记录 CPU/GPU 当前 release、服务状态、环境变量哈希和 TT Post 数据库各状态/profile 计数。
2. 校验 `/mnt/data-disk` 与 GPU `/data` 挂载，创建部署前数据库在线备份及配置/unit 备份。
3. GPU 从 GitHub 精确 commit 建立新 release，设置 `TT_POST_GPU_MEDIA_MODE=direct_outro`，保留已批准片尾 SHA，重启 `tt-gpu-publisher.service`，确认 health 返回 v2、Logo-free 资格和 asset ready。
4. CPU 从同一精确 commit 建立新 release，将 `TT_POST_MEDIA_PROFILE_VERSION` 设置为 `tt-post-direct-outro-hevc-720x1280-v2`，更新静态页和一次性迁移 unit，重启 `tt-post-service.service`。
5. 先 dry-run profile upgrade，候选数必须等于部署前 v1 available 数量；再启动 `tt-post-profile-upgrade.service`。
6. 等待重制完成，验证 pool/intake 身份一致、SQLite integrity、服务/timer/path 正常、历史发布数量不变。
7. 从新成片抽帧，确认左上角无 Logo；不触发真实发布。

## 回滚

1. 停止正在运行的一次性 profile upgrade unit。
2. CPU/GPU `current` 切回部署前 release，恢复环境文件备份并重启各自服务。
3. 如果迁移已更新数据库，先停止 TT Post service/timer/path，再从部署前 SQLite 在线备份整体恢复，随后校验 integrity 并恢复服务。
4. 禁止仅手改 profile 或 URL；必须保持 URL、GPU job ID、SHA、size、duration、trim 和 request SHA 同步。
