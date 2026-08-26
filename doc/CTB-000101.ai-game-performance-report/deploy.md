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
- 当前生产运行提交：`a63293b323f3a24afb5835d89d58e02d63b4139e`；
- 当前 release：`/opt/ai-game-performance/releases/a63293b323f3a24afb5835d89d58e02d63b4139e`；
- 上一运行提交/release：`c2ea2ac6adc746694ce2d56bf02d62ca7a1bc6cc`；
- current：`/opt/ai-game-performance/current`；
- SQLite：`/mnt/data-disk/ai-game-performance/cache/ai-game-performance.sqlite3`；
- 公开目录：`/usr/share/nginx/html/reports/ai-game-performance`；
- URL：`https://ai.yingliangads.com/reports/ai-game-performance/`；
- 最终数据版本：`20260826T165257950954+0800`；
- 首次部署前备份：`/mnt/data-disk/ai-game-performance/backups/20260825T160520+0800-pre-2655aaf`；
- 最终版本回滚备份：`/mnt/data-disk/ai-game-performance/backups/20260825T162341+0800-pre-479398b`；
- BUG-006 发布前回滚备份：`/mnt/data-disk/ai-game-performance/backups/20260825T170107+0800-pre-9804597`；
- v6 展示精简发布前回滚备份：`/mnt/data-disk/ai-game-performance/backups/20260825T181629+0800-pre-c2ea2ac`；
- 分钟单位热修复发布前回滚备份：`/mnt/data-disk/ai-game-performance/backups/20260826T164652+0800-pre-a63293b`；
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
- 最终生产：443,363 条转化、14,002 条投放事实、1,961 条总览组合；
- 2026-08-24 MySQL/SQLite 成本、安装、D1、总播放时长、收入、渠道花费/安装/曝光/点击逐项一致；
- 匿名页面/JSON 302 到正确飞书登录 next，登录态 HTML/清单/日文件为 200；
- BUG-006 后自然 timer 17:12:10 触发、17:13:19 成功，公开版本 `20260825T171308367560+0800`；
- `nginx -t`、Nginx、AI 主 API 和现有 TT 报表回归通过；
- Chrome 桌面与 390×844 手机布局、三视图、筛选、分页通过。
- v6 生产三视图、状态、质量提示、表格和导出字段契约均不再暴露“渠道行/转化行”；仅渠道维度仍聚合 6 行。

## 回滚方案

v6 回滚到上一生产提交 `98045976290b92ce3d69d030ae45eab45f386760` 时使用备份 `20260825T181629+0800-pre-c2ea2ac`，全程保留 SQLite 和新版本数据目录：

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

## v7 Unity 补数发布计划（2026-08-26，未部署）

当前生产运行提交为 `a63293b323f3a24afb5835d89d58e02d63b4139e`。以下仅为待执行计划：

1. 合并前完成独立代码评审；本地 Python/Node/前端契约/编译/格式全部绿灯。
2. 发布前记录 v6 `current/latest.json/index.html` 哈希、timer/锁/Nginx/API 状态，并备份 SQLite 与公开提交点；不得在共享刷新锁被占用时强杀或并发扫库。
3. 从 GitHub 精确提交创建不可变 release；阴影命令必须同时指定独立 `--cache-db` 与 `--output-dir`，先验证 additive `source_game_id` 迁移可重复且绝不触碰在线 SQLite。
4. 只读阴影刷新逐日读取 manual、custom delivery、Unity category 0；对账 Unity 行数、installs、starts、clicks，并单独证明 category 1 未并入。
5. 阴影 `quick_check/integrity_check`、所有日文件和对账通过后，备份在线 SQLite，切换精确代码提交，并在共享锁内对在线缓存重复全量刷新；成功发布版本文件和 `latest.json` 后再完成公开验收。阴影缓存只作证据、不直接冒充在线提交点；不重启 AI 主 API 或 TT/X/Meta 服务。
6. 验证按渠道与 `source_country` 分组：Unity 安装/曝光/点击出现，手工成本/测转安装不丢失，有效花费不双计，CPI 保持 `source_spend/source_installs=0`。

回滚边界：停止/确认 refresh inactive 后，原子切回本节发布前记录的 v6 release，恢复发布前的 SQLite 在线备份，并最后恢复备份的 `index.html/latest.json`。失败 release 和迁移后 SQLite 另存审计，不能让旧代码继续读取残留 Unity delivery/游戏映射；若不恢复 SQLite，只能由旧代码全量重建完整保留窗口。当前尚无 v7 备份路径、提交、release 或公开版本，任何此类字段只能在真实执行后补写。
