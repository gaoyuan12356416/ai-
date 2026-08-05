# 开发方案

1. GPU `direct_outro` profile 从 v1 升级到 v2，固定片尾继续做 SHA-256 pin，Logo 不再进入正式合成、manifest 或复用合同。
2. `build_phone_match_command` 将 Logo 改为显式可选输入；正式 `direct_outro` 传 `None`，历史 `branded_preview` 显式传 Logo 路径。
3. CPU `claim_recurring_run` 增加可选的精确 preparation profile 条件；服务的预领取、首次领取和恢复领取均传当前 profile。
4. 增加 profile upgrade 工具和一次性 systemd unit，只迁移精确 v1 且未占用的 available 条目。
5. 增加 GPU 命令合同、profile 过滤、迁移 dry-run/原子账本/fail-closed 测试。
6. GitHub 先提交和推送，再按精确 commit 建立 CPU/GPU immutable release；部署前做数据库与配置备份。
7. 不手动执行 TT Post runner，不发真实 TikTok 帖子；通过服务健康、数据库计数、GPU manifest/成片抽帧验证。
