# 部署文档

## 变更内容

CPU X release：selector、manual runner；GPU release：media repair；公共静态页：手动失败原因展示。

## 配置项

无配置变更。

## 数据库变更

无数据库变更；部署前仍执行在线 SQLite 备份和完整性检查。

## 部署步骤

1. 推送 GitHub commit。
2. 记录生产 hash、账本和 timer 基线，创建在线 SQLite 备份及 token 非敏感 hash/mode 清单。
3. 从该 commit 构建 CPU/GPU 不可变 release。
4. 先切 GPU 并重启 `x-post-media-repair.service`，通过 CPU 隧道检查健康。
5. 切 CPU `/opt/x-post-automation/current`；不手动启动 manual service。
6. 同步 `static/x-post-material-pool.html` 到主应用和 `/usr/share/nginx/html`。

## 验证步骤

- 生产 release 编译与针对性测试。
- GPU/CPU/main API 健康检查。
- 公共静态页 hash 与 Git release 一致。
- 自然 `x-post-manual.timer` 返回 `no_pending`。
- queue/log/unknown/active manual 与部署前基线一致；不创建真实 Post。

## 回滚方案

恢复备份中的 CPU/GPU `current` 链接与两份静态页，分别窄重启 GPU worker；若 Sidecar本次未重启则不额外重启。保留当前 SQLite、Token 和全部历史账本，不恢复数据库备份覆盖后续操作。

## 注意事项

## 生产部署结果

- Git commit / CPU release / GPU release：`eb15510b75d9045d5b660b57f3971137044a3523`。
- CPU 备份：`/mnt/data-disk/x-post-automation/backups/20260814T042057Z-manual-clear-errors-pre-eb15510`。
- GPU 备份：`/data/x-post-media-repair/backups/20260814T042112Z-manual-clear-errors-pre-eb15510`。
- CPU release 的 59 项专项测试、GPU 生产解释器的 18 项 media-repair 测试均通过。
- 主站、公共静态页、Sidecar、GPU worker 和 CPU 到 GPU 隧道健康；静态页三处 SHA-256 均为 `2fe0ab0e6d466ce8a2cc327c22162c4d3ce8d53ce2208e48b49e06d1d03a605c`。
- 自然 timer 连续返回 `no_pending`；queue/log 保持 `253/243`，unknown、active manual、active schedule、active reservation 均为 0；未创建真实 X Post。
