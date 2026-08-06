# 开发计划

## 开发范围

新增独立 Dramawave 归因对比服务、缓存刷新器、页面、测试、systemd/Nginx 部署文件和完整需求文档，不修改远程业务表。

## 任务拆分

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| SQLite schema、白名单维度/指标和公共工具 | `ops/dramawave-attribution-comparison/common.py` | 已完成 |
| 只读 MySQL 分层映射、事务刷新、裁剪和历史 cursor | `refresh_cache.py` | 已完成 |
| 聚合 API、ETag/gzip、CSV 和健康检查 | `service.py` | 已完成 |
| 响应式多维对比页面 | `index.html` | 已完成 |
| systemd timer/service、Nginx auth location | `deploy/*` / `*.conf` | 已完成并通过目标机验证 |
| 后端、前端契约和浏览器回归 | `test_*.py` + Playwright CLI | 已完成 |
| GitHub-first 发布、备份、线上 bootstrap/验收 | 部署记录 | 已完成；授权飞书会话视觉补验除外 |

## 构建 / 验证命令

```powershell
python -m compileall -q ops\dramawave-attribution-comparison
python -m unittest discover -s ops\dramawave-attribution-comparison -p "test_*.py" -v
node --check <从 index.html 抽取的内联脚本>
git diff --check
```

服务器侧：

```bash
python3 -m compileall -q .
python3 -m unittest discover -p 'test_*.py' -v
python3 refresh_cache.py --bootstrap-start 2026-07-29 --bootstrap-end "$(date +%F)"
nginx -t
systemctl status dramawave-attribution-comparison.service --no-pager
systemctl status dramawave-attribution-comparison-refresh.timer --no-pager
curl -fsS http://127.0.0.1:8832/healthz
```

## 风险与依赖

- 只读 MySQL 查询须保持日期有界并使用已核验索引。
- 初次 bootstrap 数据量大于定时近两天刷新，必须单独运行并观察资源。
- 服务端口须先核对未占用；预留默认 `127.0.0.1:8832`。
- `/mnt/data-disk` 必须是已挂载 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8` 且可写。
- 现有 TT 刷新任务正处于异常重负载状态，新服务上线不得与其做进程级耦合。
- 现有 TT 多维刷新在 `:00/:30` 查询同一只读库；本任务固定错峰到 `:22/:52`，仍保持每 30 分钟一次，并共用重任务互斥锁及 1GB cgroup 上限。

## 完成记录

- 2026-08-06：创建独立 clean worktree 和分支 `codex/dramawave-attribution-compare-20260806`。
- 2026-08-06：完成线上只读 schema、索引、覆盖日期、样本和 TT 参考架构审计。
- 2026-08-06：完成 D7/D30 映射、三层 SQLite 缓存、原子刷新、聚合 API、异步排行、响应式页面及 51 项自动化测试。
- 2026-08-06：最终本地 HTTP/Playwright fixture 通过桌面、移动、D7、渠道、优化师、分页、gzip/ETag/CSV/409 与异步排行验证。
- 2026-08-06：将 revenue union 与事实 staging 改为磁盘承载，唯一映射键只保存单一 identity、真实冲突才升级集合；增加全指标 stage 守恒、多日发布故障回滚、43 列哨兵往返和遗留 stage 清理测试。
- 2026-08-06：生产 canary 在 1 GiB cgroup 内完成，最大日 `131,010` facts、峰值约 772 MiB；全量 bootstrap `2026-07-29`～`2026-08-06` 写入 `920,751` facts。
- 2026-08-06：GitHub-first 发布 commit `e92f2aef417ce47cabfb6e3ae2056d96ad7f9894`，启用独立 Web service、`:22/:52` refresh timer、飞书鉴权 Nginx 路由和共享 TT 重任务锁。
- 2026-08-06：第一轮自然 timer 成功刷新近两天与轮转历史日，推进至 `20260806T132547Z-d44a154d`；预热后全范围常用 Campaign/Ad Set/排行接口均低于 3 ms。
