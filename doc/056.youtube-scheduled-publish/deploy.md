# 部署与回滚

目标 CPU 43.166.187.96，/root/drama_material_service；公网页面 /usr/share/nginx/html/youtube-publish.*。

使用 scripts/deploy_youtube_schedule.py，GitHub 40 位提交与 .github-verified-commit 必须匹配。安装前校验被改文件 SHA 和独立 YouTube handler SHA；app.py 仅替换目标方法，保留其他业务线上代码。核心 SQLite 采用 additive migration，备份仅作审计不得覆盖新视频事实。

仅在所有发布与生图均空闲时部署，停止 API/legacy/reviewed worker 后再次核验空闲，备份代码及两个 SQLite；安装、初始化新列、启动原服务，统一 writer 不重启。回滚先检查不存在未完结预约/未知预约意图，否则拒绝回退旧引擎；保留数据库、预约事实、资产、通知、短链。精确提交、备份、验收结果将在上线后补充。

## 2026-09-11 18:20 生产上线记录

- 分支 codex/youtube-scheduled-publish-20260911；生产与 GitHub 精确提交 `aa450e964d344151b7bb940718022685073112d0`。功能提交 517ed32206e5600590cff7595cf5b992abec0868，后续两提交为审查文档和旧 SQLite 测试夹具兼容。
- CPU Python 3.9.6：509 次用例执行，508 通过，1 条历史故障图片 fixture 不在归档中而跳过；真实符号链接与旧结构迁移测试均通过。
- 12 个安装目标，app.py 仅替换 `_dispatch_youtube_auto_publish`，原线上其他代码保留。
- 备份 `/mnt/data-disk/deploy/youtube-auto-publish/backups/scheduled-publish-20260911-182039-aa450e964d34`，内含文件清单、主 SQLite 和独立通知 outbox 审计副本。`result.json` 与 `post-deploy-facts.json` 保存安装和逐字段对比证据。
- API PID 3926926，legacy worker 3926962，reviewed worker 3926963；三个均 active/running/NRestarts=0。统一 writer PID 2994555 未变，仍 active/running/NRestarts=0。
- 原有10条发布账本、10条短链、5条准备任务和7条 failure_notice 的所有既有字段均与部署前一致；全部旧账本 publish_at=''、schedule_status='none'。
- 公网 youtube-publish.html/js/css 均 HTTP200 且原始字节等于 GitHub 归档；cachebuster `20260911-scheduled-publish-v1`。bootstrap GET 与新增 schedule POST 匿名均401。素材 SQL SHA 不变。
- 未创建测试生产任务、视频、预约、取消、封面或首评；运行时真实平台预约回执将由用户正常任务取得，mock 不代表秒级平台时效。

精确回滚命令（在 CPU 上执行）：

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/aa450e964d344151b7bb940718022685073112d0/scripts/deploy_youtube_schedule.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/scheduled-publish-20260911-182039-aa450e964d34
```

脚本会拒绝覆盖后来变更，或在存在未完成/未知预约意图时回退旧引擎。数据库、资产、通知 outbox 保留；不要用备份数据库覆盖后续真实状态。
