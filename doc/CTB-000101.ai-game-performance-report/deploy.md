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

- MySQL 无变更：全程使用 `101.32.56.53:63350` 只读端点，不执行 DDL/DML。
- SQLite 有 additive migration：在线缓存表 `delivery_fact` 幂等增加 `source_game_id TEXT NOT NULL DEFAULT ''`，不删表、不清数据；因此 v7 代码回滚必须同时恢复发布前 SQLite 在线备份。

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
- 当前生产运行提交：`28cefbb0c6439bea53b243de2595e789002dfa64`；
- 当前 release：`/opt/ai-game-performance/releases/28cefbb0c6439bea53b243de2595e789002dfa64`；
- 上一运行提交/release：`a63293b323f3a24afb5835d89d58e02d63b4139e`；
- current：`/opt/ai-game-performance/current`；
- SQLite：`/mnt/data-disk/ai-game-performance/cache/ai-game-performance.sqlite3`；
- 公开目录：`/usr/share/nginx/html/reports/ai-game-performance`；
- URL：`https://ai.yingliangads.com/reports/ai-game-performance/`；
- 最终验证数据版本：`20260826T174301241241+0800`；
- 首次部署前备份：`/mnt/data-disk/ai-game-performance/backups/20260825T160520+0800-pre-2655aaf`；
- 首次报表发布回滚备份：`/mnt/data-disk/ai-game-performance/backups/20260825T162341+0800-pre-479398b`；
- BUG-006 发布前回滚备份：`/mnt/data-disk/ai-game-performance/backups/20260825T170107+0800-pre-9804597`；
- v6 展示精简发布前回滚备份：`/mnt/data-disk/ai-game-performance/backups/20260825T181629+0800-pre-c2ea2ac`；
- 分钟单位热修复发布前回滚备份：`/mnt/data-disk/ai-game-performance/backups/20260826T164652+0800-pre-a63293b`；
- v7 Unity 补数发布前完整回滚备份：`/mnt/data-disk/ai-game-performance/backups/20260826T171700+0800-pre-28cefbb`；
- 数据盘：UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`，BUG-006 发布前可用 84 GiB；
- v7 未 reload Nginx；历次部署均未重启 `drama-material-api` 或 TT/X/Meta 发布服务。

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

- 本地与服务器：27/27 PASS，前端契约与 `node --check` PASS；
- v7 全量阴影：2026-08-10 至 2026-08-26，SQLite `quick_check/integrity_check=ok`，峰值 RSS 321,976 KiB；
- 最终生产窗口：487,895 条转化、21,568 条投放事实、2,329 条总览组合；
- 2026-08-25 Unity MySQL/SQLite/JSON 为 1,188 行、安装 11,847、曝光 2,034,406、点击 1,017,135，逐项一致且 category 1 未并入；
- 匿名页面/JSON 302 到正确飞书登录 next，登录态 HTML/清单/日文件为 200；
- v7 后自然 timer 17:42:25 触发、17:43:13 成功，公开版本 `20260826T174301241241+0800`；
- `nginx -t`、Nginx、AI 主 API 和现有 TT 报表回归通过；
- Chrome 桌面与 390×844 手机布局、三视图、筛选、分页通过。
- v6 生产三视图、状态、质量提示、表格和导出字段契约均不再暴露“渠道行/转化行”；仅渠道维度仍聚合 6 行。

## 当前 v7 回滚方案

从 v7 回滚到上一生产提交 `a63293b323f3a24afb5835d89d58e02d63b4139e` 时使用完整备份 `/mnt/data-disk/ai-game-performance/backups/20260826T171700+0800-pre-28cefbb`：

1. 停止 timer 并确认 `ai-game-performance-refresh.service` 为 inactive，再持有共享锁；
2. 将失败后的 SQLite、公开目录和 current 状态另存审计，不直接删除；
3. 恢复备份中的发布前 SQLite 在线备份；
4. 从 `current-before` 原子恢复 `/opt/ai-game-performance/current`；
5. 恢复 `public-before/index.html`，最后恢复 `public-before/latest.json` 作为提交点；
6. 启动 timer，复核 SQLite quick/integrity、公开哈希、匿名 302、登录态 200、TT 报表和 AI 主 API。

v7 的 `source_game_id` 是 SQLite additive migration，不能只切旧代码并保留迁移后的在线数据库。下列章节保留 BUG-006、v6 与分钟单位热修复的历史发布/回滚证据，不代表当前生产版本。

## 注意事项

- 部署前必须确认 `/mnt/data-disk` 是 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8` 的真实挂载且空间充足；
- 刷新与 TT/归因报表共用 `/tmp/tt_minis_multi_dim_dashboard.lock`；
- 不把完整 SQL、数据库口令或子进程参数写入日志；
- 本文件已记录本次真实发布的精确备份路径、提交、服务状态与回滚边界。

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

## v6 展示精简增量发布

- GitHub/运行提交：`c2ea2ac6adc746694ce2d56bf02d62ca7a1bc6cc`；
- release：`/opt/ai-game-performance/releases/c2ea2ac6adc746694ce2d56bf02d62ca7a1bc6cc`；
- 上一 release：`/opt/ai-game-performance/releases/98045976290b92ce3d69d030ae45eab45f386760`；
- 发布前备份：`/mnt/data-disk/ai-game-performance/backups/20260825T181629+0800-pre-c2ea2ac`，`current-before`、旧 `index.html`、旧 `latest.json` 校验通过；
- 首次锁等待因现有 TT 60 日刷新而在切换前安全超时；该进程持续消耗 CPU 并自然完成，未终止、未并发扫描；
- 锁释放后仅从既有 SQLite 执行 `--publish`，18:25:09 提交公开版本 `20260825T182509993602+0800`，再原子切换 current；
- 公开 `index.html` SHA-256 为 `36a83abdf0f9bc352f2826eff650096741f75ba4171d28a3de9b68acbf3801e6`，与 release `report.html` 一致；
- SQLite `quick_check=ok`、`integrity_check=ok`，所有清单日文件存在；内部事实计数仍为渠道 14,002、转化 443,363；
- 本地/服务器 19/19、前端不可见性契约、`nginx -t`、匿名页面/清单 302、TT 报表 302、Nginx/API/timer 状态通过；
- 登录态 Chrome 验证三视图均不含两个字段，仅渠道维度仍为 6 行；390×844 无页面横向溢出；
- 未查询 MySQL、未修改 Nginx/unit/env、未 reload Nginx，未重启 AI 主 API、TT/X/Meta 服务。

精确回滚：确认刷新 service 为 inactive 后，将 `current` 原子切回 `98045976290b92ce3d69d030ae45eab45f386760`，从本节备份恢复 `public-before/index.html` 和 `public-before/latest.json`（清单最后切换）；保留 SQLite、新 release 和新数据版本用于审计，再复核哈希、匿名鉴权、登录态页面、timer、TT 报表和主 API。

## 平均时长分钟单位热修复增量发布（2026-08-26）

- GitHub/运行提交：`a63293b323f3a24afb5835d89d58e02d63b4139e`；release：`/opt/ai-game-performance/releases/a63293b323f3a24afb5835d89d58e02d63b4139e`；上一 release 为 `c2ea2ac6adc746694ce2d56bf02d62ca7a1bc6cc`。
- 发布前备份：`/mnt/data-disk/ai-game-performance/backups/20260826T164652+0800-pre-a63293b`，旧 current、index、latest 及 SHA-256 清单校验通过。
- GitHub 精确提交经服务器 20/20、前端契约、`node --check`、`git diff --check` 后进入发布。
- 共享锁由正常 TT 60 日刷新占用时等待自然释放，未终止、绕过或并发扫库；获得锁后仅从既有 SQLite 执行 `--publish`。
- 16:52:57 提交公开版本 `20260826T165257950954+0800` 并原子切换 current；公开 index 与 release `report.html` SHA-256 均为 `e8589878723a2116c530f30fa569bec49a52a54e9b61f85b701c85a7cf915b3e`。
- SQLite `quick_check=ok`、`integrity_check=ok`，三视图各 17 个日文件完整；Nginx、主 API、timer、AI 报表鉴权和 TT 报表鉴权通过。
- 生产登录态指标卡显示 `18.13 min`，表格显示 `18.31 min` 等分钟值，可见秒单位计数为 0；内部秒口径不变。
- 未查询 MySQL、未修改或 reload Nginx，未重启 AI 主 API、TT/X/Meta 服务。

精确回滚：确认 `ai-game-performance-refresh.service` 为 inactive，持有 `/tmp/tt_minis_multi_dim_dashboard.lock` 后，将 `/opt/ai-game-performance/current` 原子切回 `c2ea2ac6adc746694ce2d56bf02d62ca7a1bc6cc`；从上述备份恢复 `public-before/index.html`，最后恢复 `public-before/latest.json` 作为提交点。保留 SQLite、新 release 和新数据版本，随后复核公开文件哈希、匿名 302、登录态秒单位、timer、TT 报表和主 API；无需 reload 或重启服务。

## v7 Unity 补数实际发布（2026-08-26）

- GitHub/生产运行提交：`28cefbb0c6439bea53b243de2595e789002dfa64`；release：`/opt/ai-game-performance/releases/28cefbb0c6439bea53b243de2595e789002dfa64`；上一 release：`a63293b323f3a24afb5835d89d58e02d63b4139e`。
- 发布前完整备份：`/mnt/data-disk/ai-game-performance/backups/20260826T171700+0800-pre-28cefbb`。`current-before`、公开 index/latest、env/unit/timer、SQLite 在线备份和 SHA-256 清单均校验通过；备份库 `quick_check=ok`。
- 共享锁先由正常 TT 60 日刷新占用，等待其自然完成；未强杀、绕过锁或并发扫库。精确 GitHub release 的服务器 27/27、前端契约、Python 编译及格式门禁全部通过。
- 独立阴影：`/mnt/data-disk/ai-game-performance/shadow/20260826T172400+0800-28cefbb`，使用隔离 cache/output 完成 2026-08-10 至 2026-08-26 全量刷新，耗时 3 分 18.63 秒、峰值 RSS 321,976 KiB；SQLite quick/integrity、所有日文件及 MySQL→SQLite→JSON 对账通过。
- 阴影全窗口 Unity category 0 为 6,530 行、安装 98,719、曝光 21,123,305、点击 11,095,086。稳定日 2026-08-25 为 1,188 行、安装 11,847、曝光 2,034,406、点击 1,017,135；category 1 未并入。
- 在共享锁内停止 timer 并确认 service inactive 后，从同一 release 对在线 SQLite 执行完整刷新，先在 rollout-stage 验证，再原子切换 index/current/latest；首次生产版本 `20260826T174015329035+0800`。公开 index 与 release HTML SHA-256 均为 `1fee30c0beb1a3e3738deffb5ec31b284562cbd0787f402021b2c3ee87684dd4`。
- 17:42:25 自然 timer 启动，17:43:13 成功退出，`Result=success`、`ExecMainStatus=0`，生成版本 `20260826T174301241241+0800`；Unity 全窗口与稳定日合计保持不变。
- SQLite quick/integrity、`nginx -t`、匿名 AI 报表/清单与既有 TT 报表 302、主 API loopback 200、登录态 Chrome 桌面和 390×844 回归均通过。未写 MySQL，未 reload Nginx，未重启 AI 主 API 或 TT/X/Meta 服务。

精确回滚：停止 timer 并确认 refresh inactive，持有共享锁后先将失败后的 SQLite/公开目录/current 状态另存审计；从上述备份恢复发布前 SQLite 在线备份，原子切回 `a63293b323f3a24afb5835d89d58e02d63b4139e`，恢复 `public-before/index.html`，最后恢复 `public-before/latest.json` 作为提交点。随后启动 timer，复核 SQLite quick/integrity、公开哈希、匿名 302、登录态页面、TT 报表和主 API。不能只切旧代码而保留 v7 迁移后的在线 SQLite。
