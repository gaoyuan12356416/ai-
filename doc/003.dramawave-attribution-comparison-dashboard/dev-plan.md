# 开发计划

## 开发范围

独立 Dramawave 归因对比服务已从 D7/D30 合同迁移为 D7/D10：新口径读取 `ads_app_revenues_10d`，API/缓存字段使用 `d10_*`，并沿用服务端缓存架构。未修改远程业务表；日期边界固定为 `2026-08-01`，生产已运行独立 D10 缓存，历史 D30 基线仅作回滚。

## 任务拆分

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| SQLite schema、白名单维度/指标和公共工具 | `ops/dramawave-attribution-comparison/common.py` | D10 已生产 |
| 只读 MySQL 分层映射、事务刷新、裁剪和历史 cursor | `refresh_cache.py` | D10 已生产；自然刷新通过 |
| 聚合 API、ETag/gzip、CSV 和健康检查 | `service.py` | D10 已生产 |
| 响应式多维对比页面 | `index.html` | D10 已生产；已授权浏览器交互通过 |
| systemd timer/service、Nginx auth location | `deploy/*` / `*.conf` | D10 已生产；timer `:04/:34` |
| 后端、前端契约和浏览器回归 | `test_*.py` + Chrome | 自动化 `64/64`；生产页面与筛选通过 |
| GitHub-first 发布、全新 D10 SQLite、原子切换和回滚 | 部署记录 | 已完成 |

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
DRAMAWAVE_ATTRIBUTION_DB_PATH='<全新D10候选SQLite绝对路径>' \
  python3 refresh_cache.py --bootstrap-start '2026-08-01' --bootstrap-end "$(date +%F)"
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
- 生产实测 TT 多维刷新在 `:13/:43` 查询同一只读库且单轮可持续约 15 分钟；D10 timer 固定错峰到 `:04/:34`，仍保持每 30 分钟一次，并共用重任务互斥锁及 1GB cgroup 上限。旧 D30 的 `:22/:52` 在 2026-08-07 连续撞锁，不能沿用。

## 完成记录

- 2026-08-06：创建独立 clean worktree 和分支 `codex/dramawave-attribution-compare-20260806`。
- 2026-08-06：完成线上只读 schema、索引、覆盖日期、样本和 TT 参考架构审计。
- 2026-08-06（历史 D30 基线）：完成 D7/D30 映射、三层 SQLite 缓存、原子刷新、聚合 API、异步排行、响应式页面及当时的 51 项自动化测试。
- 2026-08-06：最终本地 HTTP/Playwright fixture 通过桌面、移动、D7、渠道、优化师、分页、gzip/ETag/CSV/409 与异步排行验证。
- 2026-08-06：将 revenue union 与事实 staging 改为磁盘承载，唯一映射键只保存单一 identity、真实冲突才升级集合；增加全指标 stage 守恒、多日发布故障回滚、43 列哨兵往返和遗留 stage 清理测试。
- 2026-08-06（历史 D30 基线）：生产 canary 在 1 GiB cgroup 内完成，最大日 `131,010` facts、峰值约 772 MiB；全量 bootstrap `2026-07-29`～`2026-08-06` 写入 `920,751` facts。
- 2026-08-06（历史 D30 基线）：GitHub-first 发布 commit `e92f2aef417ce47cabfb6e3ae2056d96ad7f9894`，启用独立 Web service、`:22/:52` refresh timer、飞书鉴权 Nginx 路由和共享 TT 重任务锁。
- 2026-08-06（历史 D30 基线）：第一轮自然 timer 成功刷新近两天与轮转历史日，推进至 `20260806T132547Z-d44a154d`；预热后全范围常用 Campaign/Ad Set/排行接口均低于 3 ms。
- 2026-08-07：目标新口径改为 D10；只读核验 `ads_app_revenues_10d` schema/index 与 D30 相同，用户批准并固定看板起点为 `2026-08-01`。
- 2026-08-07：D10 自动化在本地和目标机均 `64/64`；独立候选库 bootstrap 8/1～8/7 共 `766,990` facts，真实唯一 Ad 精确对账、三层汇总守恒、API/CSV 和旧 D30 缓存失败关闭通过。
- 2026-08-07：GitHub-first 发布 commit `ec6fba1b7e155be904c4c54791fd491c76e09a9a`；独立 D10 SQLite 已提升为活动库，历史 D30 release/SQLite 与发布前备份原样保留作回滚。
- 2026-08-07：refresh timer 从历史 D30 的 `:22/:52` 调整为 D10 生产 `:04/:34`，避开 TT `:13/:43` 重刷新窗口。第一轮自然任务成功刷新今昨及历史轮转日并推进版本，预热完成。
- 2026-08-07：发布前备份位于 `/mnt/data-disk/dramawave-attribution-comparison/backups/20260807-175041-pre-6c323f5ff35e`；已授权 Chrome 页面加载、Meta 筛选和 D7 指标切换通过。
