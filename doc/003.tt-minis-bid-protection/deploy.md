# 部署文档

## 变更内容

- 新增 `ads_ai.ads_tiktok_minis_bid_protection_daily`。
- 新增独立 TT 小程序 Bid Protection Campaign 同步脚本和北京时间每日 `09:25/21:25` root cron。
- 安全替换 `/root/codex_test/tt_business_api_tokens.sqlite3` 的 `native_growth_default` Token。

## 配置项

- TikTok Token：只从服务器 SQLite Token 库读取，不进入环境模板或 Git。
- MySQL：运行写入走 `101.32.56.53:63353`；验收只读走 `101.32.56.53:63350`；目标库固定 `ads_ai`。
- 调度：root crontab；独立锁 `/tmp/tt_minis_bid_protection_sync.lock`；独立日志 `/mnt/data-disk/tt-minis-bid-protection/logs/tt_minis_bid_protection_sync.log`。
- 时区：CPU 主机固定 `Asia/Shanghai`，每日 `09:25`、`21:25`。

## 数据库变更

1. 在写入口先验证 `DATABASE()='ads_ai'`、`@@read_only=0`、MySQL 版本和目标表不存在。
2. 执行 `ops/tt-minis-bid-protection/001_create_ads_tiktok_minis_bid_protection_daily.sql`。
3. 通过只读入口读回 `SHOW CREATE TABLE`、`SHOW INDEX` 和 `COUNT(*)=0`。
4. 同步脚本正常运行仅对固定表执行 `SELECT/INSERT/UPDATE`，运行时不执行 DDL。

## 部署步骤

1. 本地确认工作树无无关改动，运行 Python 编译、单元测试和 `git diff --check`；提交并推送 GitHub。
2. CPU 服务器记录现有部署提交、相关进程/cron 状态、SQLite 文件权限和 Token 行哈希；将现有 release/current、root crontab 及 Token 库分别备份到数据盘时间戳目录。
3. 通过 GitHub 拉取已验证的精确提交到 `/mnt/data-disk/tt-minis-bid-protection/releases/<commit>`；将 `/mnt/data-disk/tt-minis-bid-protection/current` 原子切换为直接指向该 release 内的 `ops/tt-minis-bid-protection` 模块目录。cron 因此可在 `current` 下直接调用脚本；禁止直接复制本地源码伪装发布。
4. 使用 SQLite Backup API 创建一致性 Token 库备份。通过交互式隐藏输入/标准输入将新 Token 仅保存在内存中，先调用 Bid Protection status、history 和现有 Native Growth 三类只读 canary。
5. canary 全部通过后，以 `BEGIN IMMEDIATE` 和旧值哈希条件更新唯一的 `native_growth_default` 行；验证影响行数恰好为 1，回读只比较哈希，不输出 Token。
6. 执行单表 DDL并完成只读读回；先不写入新 cron。
7. 将旧事实行流式导出到数据盘 gzip 文件，记录行数和 SHA-256，确认可读后清空且仅清空目标表；同时备份并清理旧重试状态。
8. 手工执行 `python3 tt_minis_bid_protection_sync.py --backfill-days 30`。回填可重跑，按日/账户/Campaign 分批，任何失败不得清空成功行。
9. 创建权限受限的独立日志目录。完成重复运行幂等、三产品账户覆盖、Campaign 单层、金额缩放和 DramaWaveMinis `2026-09-02` 样本验收后，安装以下 cron 并读回确认：

```cron
25 9,21 * * * /usr/bin/flock -xn /tmp/tt_minis_bid_protection_sync.lock -c "cd /mnt/data-disk/tt-minis-bid-protection/current && /usr/bin/python3 tt_minis_bid_protection_sync.py --daily" >> /mnt/data-disk/tt-minis-bid-protection/logs/tt_minis_bid_protection_sync.log 2>&1
```

## 验证步骤

```bash
python3 -m py_compile ops/tt-minis-bid-protection/tt_minis_bid_protection_sync.py
python3 -m unittest discover -s ops/tt-minis-bid-protection -p 'test_*.py'
git diff --check
python3 ops/tt-minis-bid-protection/tt_minis_bid_protection_sync.py --start-date 2026-09-02 --dry-run
crontab -l
```

数据库验收至少检查：

- `SHOW CREATE TABLE` 和 `SHOW INDEX` 与 DDL一致。
- 重跑同一日期前后唯一粒度计数不增长。
- `SUM(credit_amount_scaled) / 100000 = SUM(credit_amount)` 按币种成立。
- 账户 SQL 当前返回 916 个账户，`minis_id` 分布完整；产品集合包含 3346、3380、3416，`data_level` 仅为 `CAMPAIGN`。
- `2026-09-02` DramaWaveMinis 仅以 Campaign 层按币种汇总，另输出明细和失败账户数。
- 独立 cron 日志与进程 argv 不含新旧 Token、数据库密码或完整鉴权头。

## 2026-09-03 实际部署记录

已完成：

- GitHub 精确 release：`8668e31373e592b34538fc911d88fa14caa2fa28`；旧 release `2235001` 与 `8ede1c8` 均保留作为代码回滚点。
- 自动化回归：24 项测试全部通过。
- 生产 DDL：只读入口读回 18 列、1 个业务唯一键和 5 个二级索引，结构与版本库 DDL 一致。
- Token 轮换：全量兼容 canary 覆盖产品 3346 的 356 个账户、3380 的 68 个账户、3416 的 148 个账户，共 572 个账户；Bid Protection status、history 和现有 Native Growth 在写入前后均通过。
- SQLite 一致性备份：`/mnt/data-disk/tt-minis-bid-protection/backups/token/tt_business_api_tokens.sqlite3.20260903T041332Z.before_bid_protection`。
- 两次旧写入性能试跑均已安全终止，随后确认目标表仍为 0 行；未留下半批数据。第二次试跑定位到 `NOW()` 使 PyMySQL `executemany` 退化为逐行写入，已改成全参数占位的 500 行批量提交实现。

原方案暂停时尚未完成：

- 最近 60 天首次回填（已被 30 天重建方案替代）。
- 同范围重复运行的幂等读回。
- 三产品、Campaign/Ad Group 两层的最终落表覆盖检查（已改为 Campaign 单层）。
- 每日 `09:25` root cron 安装与自然触发（已改为 `09:25/21:25`）。
- DramaWaveMinis `2026-09-02` 金额、Campaign 明细及失败账户数输出。

## 回滚方案

1. 先从 root crontab 删除且仅删除本任务的精确一行，并确认没有本任务进程持锁，不影响其他 TT 任务。
2. 将 `current` 软链切回已保留的上一 release `223500167e17edbdc1a8c727c7a6851eaeb7495e` 并读回软链目标；当前 release 为 `8668e31373e592b34538fc911d88fa14caa2fa28`。
3. Token 新值不可用时，仅用备份值/旧哈希做单行 CAS 恢复并重跑三类只读 canary；不得用整库覆盖，以免丢失并发更新。
4. 若需要回滚本次数据重建，从本次清表前 gzip 备份恢复目标表；不得影响其他 `ads_ai` 表。
5. 记录备份目录、回滚提交、Token 哈希（非值）、服务状态和验证结果。

## 注意事项

- root crontab 只新增本任务的精确一行，不修改现有 TT 小程序看板/播报 cron。
- 首次 30 天回填需人工执行且观察 API 限流；cron 后续每天运行两次，每次处理最近 14 个完整自然日。
- DDL 注释使用 ASCII，避免远程传输导致注释乱码。
- Token 轮换是共享变更，若任一 canary 失败，保持或恢复旧记录并停止部署。
