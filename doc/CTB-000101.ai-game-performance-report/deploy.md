# 部署文档

## 变更内容

- 新增独立 AI 游戏报表生成器和静态页面；
- 新增 `/reports/ai-game-performance/` Nginx 鉴权 location；
- 新增 oneshot 刷新 service 与 30 分钟 timer；
- 新增 `/mnt/data-disk/ai-game-performance` 缓存、版本文件和备份目录。

## 配置项

配置文件 `/etc/ai-game-performance.env`（`0600 root:root`），只保存非公开运行配置或引用现有本机只读 MySQL 命令；真实口令不进入 GitHub/日志。

可配项：`AI_GAME_REPORT_DATA_ROOT`、`AI_GAME_REPORT_WEB_DIR`、`AI_GAME_REPORT_RETENTION_DAYS`、`AI_GAME_REPORT_REFRESH_DAYS`。

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

## 验证步骤

实际命令、输出摘要、提交和路径在部署后补充；至少包括：

```bash
python3 -m py_compile /opt/ai-game-performance/current/ops/ai-game-performance/ai_game_performance_dashboard.py
python3 -m unittest discover -s /opt/ai-game-performance/current/ops/ai-game-performance -p 'test_*.py' -v
sqlite3 /mnt/data-disk/ai-game-performance/cache/ai-game-performance.sqlite3 'PRAGMA quick_check;'
nginx -t
systemctl status ai-game-performance-refresh.timer --no-pager
curl -sS -I https://ai.yingliangads.com/reports/ai-game-performance/
```

## 回滚方案

- 停止/禁用新 timer；
- 恢复备份的 Nginx/unit/env，若首次部署则移走对应新文件；
- 将 `/opt/ai-game-performance/current` 切回上一个提交或移除首次部署 symlink；
- `systemctl daemon-reload && nginx -t && systemctl reload nginx`；
- 保留 SQLite 和公开版本目录供诊断，不删除 MySQL 数据；
- 重新验证现有 TT 报表和 AI 主 API。

## 注意事项

- 部署前必须确认 `/mnt/data-disk` 是 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8` 的真实挂载且空间充足；
- 刷新与 TT/归因报表共用 `/tmp/tt_minis_multi_dim_dashboard.lock`；
- 不把完整 SQL、数据库口令或子进程参数写入日志；
- 本文件将在真实部署后补充精确备份路径、提交、服务状态与回滚命令。
