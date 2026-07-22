# 部署文档

## 变更内容

- 新增 `ads_ai.tiktok_personal_account_snapshot`。
- 新增独立 SocialKit -> ads_ai 同步脚本。
- 新增 systemd oneshot 与 timer，每小时第 5 分钟运行。

## 配置项

真实值只存 `/etc/socialkit-tiktok-account-sync.env`，root:root 0600。模板见 `deploy/socialkit-tiktok-account-sync.env.example`。

| 前缀 | 用途 |
| --- | --- |
| `SOCIALKIT_TT_SOURCE_MYSQL_*` | 外部 SocialKit 只读源，固定 28914/socialkit |
| `SOCIALKIT_TT_TARGET_MYSQL_*` | ads_ai 写目标，固定 63353/ads_ai |
| `SOCIALKIT_TT_SYNC_MAX_ROWS` | 源行安全上限，最大 1000 |
| `SOCIALKIT_TT_SYNC_LOCK_FILE` | 单实例锁，固定 `/run/lock` 下 |

## 数据库变更

通过 63353 手工执行一次 `001_create_tiktok_personal_account_snapshot.sql`。运行时代码不执行 `CREATE/ALTER/DROP`。执行前确认 `DATABASE()='ads_ai'` 与 `@@read_only=0`，执行后通过 63350 回读表结构。

## 部署步骤

1. 本地测试、commit 并 push `codex/socialkit-tiktok-account-sync-20260722`。
2. CPU 服务器验证 GitHub SSH，创建精确 commit 的 release checkout。
3. 备份现有同名 unit/env/symlink（若存在）和 crontab；记录 API 服务状态。
4. 通过 63353 执行一次 DDL；63350 回读列、索引和空表状态。
5. 用 root-only 方式创建 `/etc/socialkit-tiktok-account-sync.env`，不得打印密钥。
6. 将稳定 symlink `socialkit-tiktok-account-sync-current` 指向精确 release。
7. 安装 service/timer，`systemctl daemon-reload`。
8. 先执行 `--dry-run`，再手工启动 oneshot；通过 63350 脱敏回读。
9. 再次启动 oneshot验证幂等；最后 enable/start timer。
10. 确认 `drama-material-api.service` 和现有 crontab 未变。

## 验证步骤

```bash
python3 -m py_compile scripts/sync_socialkit_tiktok_accounts.py
python3 scripts/test_sync_socialkit_tiktok_accounts.py
systemd-analyze verify deploy/socialkit-tiktok-account-sync.service deploy/socialkit-tiktok-account-sync.timer
systemctl start socialkit-tiktok-account-sync.service
systemctl status socialkit-tiktok-account-sync.service --no-pager
systemctl enable --now socialkit-tiktok-account-sync.timer
systemctl list-timers socialkit-tiktok-account-sync.timer --no-pager
```

数据库回读只返回行数、指标汇总、Token 非空数量和状态计数，不选择 Token 原文。

## 回滚方案

- 立即停止并禁用 timer：`systemctl disable --now socialkit-tiktok-account-sync.timer`。
- 代码回滚：将 stable symlink 恢复到上一 release；首次部署则移除 symlink，并恢复/移除新增 unit。
- 配置回滚：从部署前备份恢复 env；首次部署可在 timer 停止后移走 env。
- 数据表默认保留用于审计；不得直接 DROP。若必须撤销，先重命名/导出并另行审批。
- 任何回滚都不得把 `is_active=0` 行的旧 Token 恢复为可用。

## 注意事项

- 生产目录不是 Git 仓库，禁止整仓覆盖现网。
- 不重启主 API；本需求只安装独立 oneshot/timer。
- 不在日志、journal、命令行或部署文档打印密码和 Token。
- 当前写账号权限较宽，后续应由 DBA 建立目标表最小权限账号。

## 生产部署记录

待部署后补充 commit、release、备份目录、DDL 回读、首次/二次同步和 timer 证据。
