# 部署文档

## 变更内容

- 新增独立 AI 游戏报表生成器和静态页面；
- 新增 `/reports/ai-game-performance/` Nginx 鉴权 location；
- 新增 oneshot 刷新 service 与 30 分钟 timer；
- 新增 `/mnt/data-disk/ai-game-performance` 缓存、版本文件和备份目录。

## 配置项

配置文件 `/etc/ai-game-performance.env`（`0600 root:root`），只保存非公开运行配置或引用现有本机只读 MySQL 命令；真实口令不进入 GitHub/日志。

可配项：`AI_GAME_REPORT_DATA_ROOT`、`AI_GAME_REPORT_WEB_DIR`、`AI_GAME_REPORT_BASE_MODULE_DIR`、`AI_GAME_REPORT_RETENTION_DAYS`、`AI_GAME_REPORT_REFRESH_DAYS`。

## 数据库变更

无。MySQL 全程使用 `101.32.56.53:63350` 只读端点；不执行 DDL/DML。

## 部署步骤

1. 记录当前 GitHub 提交、Nginx/TT 报表状态、timer 状态和 `/mnt/data-disk` 挂载/空间。
2. 创建时间戳备份目录，保存旧 Nginx/unit/env/当前 symlink；首次部署记录 `missing-before`。
3. 在 `/opt/ai-game-performance/releases/<commit>` 检出 GitHub 精确提交，校验 SHA。
4. 在数据盘阴影目录执行首次全量刷新、SQLite `quick_check`、JSON/HTML 契约和只读对账。
5. 安装 Nginx 配置与 systemd unit，运行 `nginx -t`、`daemon-reload`。
6. 原子切换 `current` symlink 和公开文件，reload Nginx，启动 timer；不重启 AI 主 API。
7. 执行生产 HTTP、浏览器、数据和回归验证。

## 实际部署记录

- 分支：`codex/ai-game-performance-report-20260825`；
- 当前生产运行提交：`98045976290b92ce3d69d030ae45eab45f386760`；
- 当前 release：`/opt/ai-game-performance/releases/98045976290b92ce3d69d030ae45eab45f386760`；
- 初始运行提交/上一 release：`479398bed4a2656a431d571ec3b58d0efd452a88`；
- current：`/opt/ai-game-performance/current`；
- SQLite：`/mnt/data-disk/ai-game-performance/cache/ai-game-performance.sqlite3`；
- 公开目录：`/usr/share/nginx/html/reports/ai-game-performance`；
- URL：`https://ai.yingliangads.com/reports/ai-game-performance/`；
- 最终数据版本：`20260825T171308367560+0800`；
- 首次部署前备份：`/mnt/data-disk/ai-game-performance/backups/20260825T160520+0800-pre-2655aaf`；
- 最终版本回滚备份：`/mnt/data-disk/ai-game-performance/backups/20260825T162341+0800-pre-479398b`；
- BUG-006 发布前回滚备份：`/mnt/data-disk/ai-game-performance/backups/20260825T170107+0800-pre-9804597`；
- 数据盘：UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`，BUG-006 发布前可用 84 GiB；
- Nginx 仅 reload；未重启 `drama-material-api` 或 TT/X/Meta 发布服务。

## 验证步骤

最终验证命令：

```bash
python3 -m py_compile /opt/ai-game-performance/current/ops/ai-game-performance/ai_game_performance_dashboard.py
python3 -m unittest discover -s /opt/ai-game-performance/current/ops/ai-game-performance -p 'test_*.py' -v
sqlite3 /mnt/data-disk/ai-game-performance/cache/ai-game-performance.sqlite3 'PRAGMA quick_check;'
nginx -t
systemctl status ai-game-performance-refresh.timer --no-pager
curl -sS -I https://ai.yingliangads.com/reports/ai-game-performance/
```

结果摘要：

- 本地与服务器：19/19 PASS，前端契约与 `node --check` PASS；
- 全量阴影：2026-08-10 至 2026-08-25，SQLite `quick_check=ok`，峰值 RSS 310,036 KiB；
- 最终生产：440,857 条转化、13,996 条投放事实、1,960 条总览组合；
- 2026-08-24 MySQL/SQLite 成本、安装、D1、总播放时长、收入、渠道花费/安装/曝光/点击逐项一致；
- 匿名页面/JSON 302 到正确飞书登录 next，登录态 HTML/清单/日文件为 200；
- BUG-006 后自然 timer 17:12:10 触发、17:13:19 成功，公开版本 `20260825T171308367560+0800`；
- `nginx -t`、Nginx、AI 主 API 和现有 TT 报表回归通过；
- Chrome 桌面与 390×844 手机布局、三视图、筛选、分页通过。

## 回滚方案

最终版本回滚到上一生产提交 `2655aaf` 时使用备份 `20260825T162341+0800-pre-479398b`，全程保留 SQLite 和新版本数据目录：

1. `systemctl stop ai-game-performance-refresh.timer`；
2. 从备份的 `current-before` 创建临时 symlink，再用 `mv -Tf` 原子恢复 `/opt/ai-game-performance/current`；
3. 将备份 `public-before/index.html` 与 `latest.json` 分别安装为同目录临时文件，先切 index、最后切 `latest.json` 提交点；
4. 恢复备份的 env、Nginx 配置和两个 systemd unit；
5. `systemctl daemon-reload && nginx -t && systemctl reload nginx`；
6. `systemctl start ai-game-performance-refresh.timer`，复核匿名 302、登录态 200、TT 报表和 AI 主 API。

若需要完全撤销首次部署，先停止/禁用 timer，再把新 Nginx/unit/env/current/public 移入新的回滚备份目录，不直接删除；恢复前状态后执行 `daemon-reload`、`nginx -t` 和 reload。

## 注意事项

- 部署前必须确认 `/mnt/data-disk` 是 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8` 的真实挂载且空间充足；
- 刷新与 TT/归因报表共用 `/tmp/tt_minis_multi_dim_dashboard.lock`；
- 不把完整 SQL、数据库口令或子进程参数写入日志；
- 本文件将在真实部署后补充精确备份路径、提交、服务状态与回滚命令。

## BUG-006 验收缺陷增量发布

- GitHub/运行提交：`98045976290b92ce3d69d030ae45eab45f386760`；
- release：`/opt/ai-game-performance/releases/98045976290b92ce3d69d030ae45eab45f386760`；
- 上一 release：`/opt/ai-game-performance/releases/479398bed4a2656a431d571ec3b58d0efd452a88`；
- 发布前备份：`/mnt/data-disk/ai-game-performance/backups/20260825T170107+0800-pre-9804597`；
- 17:01 使用共享锁从既有 SQLite 执行无 MySQL 的 `--publish`，生成提交点 `20260825T170107260821+0800`；公开 `index.html` SHA-256 与 release `report.html` 一致；
- 未修改 Nginx/unit/env，未 reload Nginx，未重启 AI 主 API、TT/X/Meta 服务；timer 持续 enabled/active；
- 本地/服务器 19/19、前端契约、`nginx -t`、匿名页面/清单 302、TT 报表 302、主 API loopback 200、SQLite quick/integrity 通过。
- 17:12:10 自然刷新覆盖 8 月 23 至 25 日，17:13:19 成功提交 `20260825T171308367560+0800`；8 月 24 日源库/SQLite 逐字段一致（838 条渠道事实、48,088 条转化事实），登录态页面按渠道聚合为 6 行。

精确回滚：确认刷新 service 为 inactive 后，将 `current` 原子切回上一 release，并从上述备份恢复 `public-before/index.html` 与 `public-before/latest.json`（`latest.json` 最后切换）；无需恢复 SQLite。随后复核文件哈希、匿名 302、登录态页面、timer 和 TT/主 API。若新 release 已产生后续完整快照，只回滚代码/清单提交点，保留 SQLite 与版本目录用于诊断。
