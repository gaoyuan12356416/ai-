# 部署与回滚

目标：43.166.187.96 `/root/drama_material_service`，静态同时更新 `/usr/share/nginx/html`。代码先push GitHub，再用 `/root/codex_repos/ai-.git` fetch核验提交并创建独立release worktree。

执行 release 中 `python3 scripts/deploy_youtube_channel_templates.py --check`，再运行同脚本不带参数。前置检查数据盘UUID、干净Git工作目录和线上文件SHA。在线SQLite backup以及改动文件/两份导航备份写入 `/mnt/data-disk/deploy/youtube-auto-publish/backups/channel-templates-<timestamp>-<sha>`。

脚本只追加现有YouTube导航，继承原入口权限，保留其他分组和线上quick-nav补丁；停止API后安装改动并重新启动API。发布worker/旧worker/统一writer不执行重启或启用。数据库建表由新服务懒初始化完成，已有表不迁移、不回填。

核验：API健康、新页面/资源200并核对SHA、匿名接口401、已有操作者会话只读GET列表与模板、旧发布文案及短链/账本计数、服务PID。禁止自动创建测试发布/评论。

回滚：`python3 <release>/scripts/deploy_youtube_channel_templates.py --rollback <backup>`。脚本只恢复本次代码/静态/导航并重启API，保留SQLite、模板行、所有发布账本、短链及资产；存在后续代码或导航漂移时拒绝覆盖。部署执行证据另记 deployment-evidence.md。
