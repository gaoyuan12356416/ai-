# 部署与回退

基线c7489e1（运行文件等同已部署e59fd0b）。沿用43.166.187.96主API、FB runtime及静态站点，GitHub-first。

scripts/deploy_fb_group_page_languages.py限定变更白名单、逐目标SHA预检。停止新组调度后排空原生产者和租约，备份现库/文件，在副本排练迁移；实际迁移比较既有fb_video/fb_group表全部旧列的行数与哈希，保留现计划和任务，窄重启并恢复全部原状态。部署前后避开已启用新旧池时点。

## 已部署

- 2026-10-26 18:17 北京时间，GitHub提交 `ac02cbf7640961e33a5ca402f8b8a530748fa96e`，分支 `codex/fb-group-material-pool-20261026`，PR18。
- 主机 `43.166.187.96`，精确检出 `/mnt/data-disk/fb-video-publish/releases/group-languages-ac02cbf`。
- 21个目标文件安装后SHA一致，包含主API和FB runtime的6个Python模块、三处HTML/JS/CSS；主 `app.py` 不变。
- 备份目录 `/mnt/data-disk/fb-video-publish/backups/group-languages-20261026T101714Z-ac02cbf7/`，含文件备份、`operational.sqlite3`、迁移排练副本、`manifest.json`（phase=deployed）及 `readonly-final-ledger.json`。
- 新增两列，17张fb_video/fb_group表的全部旧列在迁移期间行数/哈希一致，SQLite quick_check=ok。原API、FB API、worker、旧单Page池调度及新组池调度5个服务均active，原启用状态恢复。
- 上线后3个公开静态文件HTTP200且SHA与提交一致，未认证目标Page接口401；真实读取及当前登录浏览器验收见test-report。
- 新组池6张表仍0行，没有保存配置、启用或创建测试任务。验收期间其他使用者/流程新增60条 `source_kind=manual` 任务（未来日期），未干预；备份中的328条既有任务全部字段、328条短链全部字段均保持原值。不要把并发正常业务写入当作迁移修改或恢复旧库覆盖它们。

## 功能回退

只关闭新组池写入/调度；保留兼容v1/v2执行器、现库、已接受任务与短链。回退前检查当前后端SHA，已有新版本漂移会拒绝执行，需先核对。

```bash
cd /mnt/data-disk/fb-video-publish/releases/group-languages-ac02cbf
python3 scripts/deploy_fb_group_page_languages.py --rollback /mnt/data-disk/fb-video-publish/backups/group-languages-20261026T101714Z-ac02cbf7/manifest.json
```

禁止恢复旧SQLite或删除新短链；回退后只读记录入口与发布人权限保留。技能上下文未修改，规则和验收证据保存在本需求目录。
