# 部署文档

## 变更内容

TT sidecar/core/link helper、Nginx TT snippet、TT 发布池页面。

## 数据库变更

无 schema 或手工数据变更。

## 部署前备份

- 当前 `/opt/tt-post/current` release。
- SQLite online backup 与 integrity check。
- `/mnt/data-disk/tt-post-publisher/s2l`、`/mnt/data-disk/x-post-automation/s2l` 文件清单/hash。
- Nginx TT snippet、三份 TT 静态页。

## 部署步骤

1. 推送并核对 GitHub commit。
2. 创建不可变候选 release，运行编译和全量 TT 测试。若候选目录由 `mktemp -d` 创建，归档解压并去除写权限后必须把 release 顶层目录设为 `0555` 或 `0755`，并用 `tt-post` 用户验证目录可遍历、入口脚本可读。
3. 更新精确 Nginx snippet，`nginx -t`。
4. 切换 release，同步静态页，重启 `tt-post-service.service`，reload Nginx。
5. health、公网页面、新旧路由合同和数据库业务字段只读验收。

## 回滚

切回上一 release，恢复 Nginx snippet/静态页并重启 sidecar、reload Nginx。无 schema 变化，普通回滚不恢复 SQLite。

## 生产结果

- GitHub 代码提交：`e11305771246dea484f3a11c5a62dfc46a60b9fb`。
- 生产 release：`/opt/tt-post/releases/e11305771246dea484f3a11c5a62dfc46a60b9fb`。
- 备份：`/mnt/data-disk/tt-post-publisher/backups/20260804T063705Z-32f1f28-to-e113057-disclosure-short-link`，SQLite `integrity_check=ok`。
- 候选机验证：Python 编译、`test_tt_post_links.py` 7/7、`test_tt_posts_core.py` 70/70。
- 服务：`tt-post-service.service`、runner/prepare timer、Nginx 均为 active；`/health` 返回 `ok=true`；Nginx 配置检查通过。
- 路由：新 `/s2l/tt/8.html` 在尚无文件时 404、非法 ID 404、POST 403；新 Nginx 精确路由和旧 19 位 TT 路由同时存在。
- 历史保护：公开 X `/s2l/6.html` hash 仍为 `8aca17e...1717a`；旧 TT `/s2l/8000000000000000005.html` hash 仍为 `0e17236...d4d9`；X 主配置 hash 仍为 `f6da43c...72a24`。
- 数据库：部署前后 queue CSV 无差异，`integrity_check=ok`，7 条历史 queue、0 条活动 queue；未创建任务、未调用真实 TikTok 发布。
- 14:40 自然 runner tick 成功，`publish_request_count=0`、`direct_publish_count=0`、`status=ok`。
- 部署问题：首次候选 release 顶层为 `0500`，sidecar 因运行用户不可遍历而重启失败；修正为 `0555` 后恢复，14:40 起无新错误。详见 `bugs/BUG-002.md`。
