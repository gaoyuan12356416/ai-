# X 随机视频模板开关（2026-10-19）

已上线，仅 X：自动模板编辑、素材池定时、短剧池定时、素材池手动发布弹窗增加 `random_template_enabled`，全部默认 false，严格布尔值；仅视频生效。手动每批重新关闭，各入口独立。

版本配置/随机日计划/运行批次/队列逐级冻结，旧任务全部默认关闭，不重写冻结值。手动幂等指纹包含开关。CPU 在凭证锁外完成原解析/必要修复/路由，再调用 GPU `/internal/x-post-random-template`（已有 18820 隧道和 token），重新核查资格后上传。GPU 复用既有随机素材、原片透明层和 OpenCL/NVENC，但独立任务/COS 命名空间，和原 X 修复共用并发锁。

首次固定请求、目录 SHA256 和配方，CPU 制作表 `x_post_random_template_preparation` 保存指纹及回执；重复请求复用同一成片。失败、超时、指纹不符禁止改发原片；已有发布尝试/未知结果保持原防重复保护。图片跳过。随机制作不裁短视频，140 秒边界、会员 4 小时、512 MiB、自动模板 600 秒门禁不变；Premium 只对源 Post 制作，目标继续转发。

当前 CPU release `9313f9fd8db577f9e1d4106fcd0998b891af8a60-x-random`（`/opt/x-post-automation/current`）；GPU `1206a815e8491b619b47dca49471023670771daf-x-random`（真实入口 `/data/x-post-media-repair/current`，不要误用旧 `/opt` 指针）。GPU worker 停止会连带停止 `x-post-media-repair-tunnel.service`，部署/回滚必须一起恢复隧道。不要动 FB/TT 服务。

维护源码/部署白名单/验收/回滚见 ai- 仓库分支 `codex/x-random-template-switch-20261019` 的 `doc/x-random-template-switch/`、`scripts/deploy_x_random_template.py` 和 `scripts/verify_x_random_template_live.py`。真实 GPU 已验收 12.5 s、51.78 s 短剧和 150 s 长视频，X 上传全部模拟。生产历史队列和账本行内容迁移前后一致。

回滚保留数据库、token、账本、配方和成片；先检查启用配置及待执行任务。存在开启任务时先暂停 X 定时器/发布入口并核查精确批次，禁止让旧代码绕过随机制作或把冻结开关改 0。回滚脚本对此阻断。CPU 备份 `/mnt/data-disk/x-post-automation/backups/20261019T101459-x-random-9313f9fd8db5`；GPU 备份 `/data/x-post-media-repair/backups/20261019T100516-x-random-1206a815e849`。
