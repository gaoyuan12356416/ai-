# 部署与回滚

## 发布边界

仅三条随机模板服务及 CPU 配方生成/UI。GitHub 精确提交先推送，服务器从 origin 取回对应提交；独立 release 目录测试后再切换 current。保持素材目录与编码 profile。

CPU 当前目录不是 Git 仓库，必须从独立 GitHub checkout 取 allowlist 文件；目标仅 features/drama_synthesis/core.py、static/index.html 和 nginx index.html。备份前比较已采集 SHA，禁止整目录覆盖。

## 固定发布版本

| 链路 | GitHub 分支 | runtime 提交 |
| --- | --- | --- |
| Drama / CPU UI | `codex/random-source-overlay-drama-20260920` | `223577b41466d03fd330625441ab3b7c1d86e843` |
| TT 随机模板 | `codex/random-source-overlay-tt-20260920` | `80acd8b4f082e121f8332dea2a34f23d739b1bb3` |
| FB 随机模板 | `codex/random-source-overlay-fb-20260920` | `96dc90d1d59622098fa36493862e37e485b84545` |

三条分支均已推送 `https://github.com/gaoyuan12356416/ai-.git`。Drama 分支后续只有运维脚本与验收文档提交时，runtime 仍使用上述精确提交。素材集合 SHA 保持 `b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c`。

## 切换

1. 新发布目录预检及离线渲染成功。
2. 保存 CPU/GPU 服务、指针、代码 SHA、定时器状态和配置备份。
3. TT maintenance gate/pause；暂停 FB 新领取；现有 GPU 请求自然完成，确认空闲。
4. 切换三路 GPU 指针和 Drama release SHA，启动并验证能力、保留的 profile/catalog、所有隧道。
5. 原子更新 CPU allowlist，重启 API/worker，检查生成配方和公共 UI。
6. 恢复本次暂停的领取和原定时器状态；留下健康与最终读取证据。

CPU 备份与状态目录：`/mnt/data-disk/random-overlay-gpu/backups/20260920-source-overlay`；HK 对应目录：`/data/random-overlay-gpu/backups/20260920-source-overlay`。`state-before.json` 保存原文件散列、服务和指针，`switch-after.json` 保存切换后读回。

Drama release SHA 必须通过最终加载的 `EnvironmentFile=/etc/random-overlay-subtemplates/drama-source-overlay-20260920.env` 设置；仅写 `Environment=` 会被先前的 catalog 环境文件覆盖。

本次另有两个已经在执行的 prepare-only 运维客户端暂时暂停。除 `release.py resume` 恢复常规 FB 领取及 TT 触发器外，必须执行 CPU 备份目录的 `extra_prepare_clients.py resume`，恢复 `extra-prepare-clients.json` 中记录的两个 PID 身份。`audit_cpu_final.py` 会核对三个暂时暂停的客户端、原有 timer/path 状态和 TT maintenance gate 均已恢复，不以单纯的恢复命令退出码作为完成凭据。

## 回滚

恢复本次记录的旧指针/系统服务 drop-in 与 CPU allowlist 备份；只重启受影响服务及隧道。回滚前停新领取并排空。新增冻结配方不得交给不支持新字段的旧 renderer：保留已验证新 GPU renderer 完成既有新配方，先回退新配方生成/UI，必要时维持暂停等待修复。不得回滚业务数据库、历史 manifest、源收据或发布账本。

此次发布已完成。执行时间、进程、提交、备份、入口恢复与自然新配方读回见 test-report.md。旧 GPU 回滚点为 Drama `9c4c3cb4eca260d46df8ab23443a32cda667c20c`、TT `326e16defb8f0b1aec76e8f36d52bb6359275a98-catalog` 目录、FB `e3bef98` 目录；CPU 回滚点为上述 CPU 备份目录的 `files/` 与 `public-index.html`。新冻结配方已经产生，适用上方保留兼容 renderer 的回滚限制。
