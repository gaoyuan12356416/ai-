# 测试报告

## 测试结论

本地与 CPU 服务器自动化回归均通过，生产关闭态部署和 canary 已完成。素材校验与隔离账本入池均在 5 秒内返回，且入池前 Creator Info/GPU 调用为 0；生产真实发布门禁保持关闭，未产生 TikTok init/post。

发布门槛：

- 全部 P0/P1 自动化通过；
- SQLite migration 在备份副本与生产均 `integrity_check=ok`；
- 生产真实 preview 与生产环境隔离账本 queued canary 成功；ready 原子迁移由自动化覆盖；
- 三个发布 gate 始终为 0；
- canary 期间真实 TikTok init/post 数为 0。

## 测试范围

- Core：intake schema、幂等、去重、FIFO、lease/fencing、retry、原子 complete。
- Service：快速 preview、queued add、合并 list、prepare 内部 API、账号实时限制。
- Runner：配置、loopback、防并发、claim/renew/process、心跳清理。
- UI：批量快速校验、立即入池、状态轮询与安全渲染。
- 回归：TT account/settings/schedule/run-now/publish/reconcile/app routes。
- 生产关闭态：服务、SQLite、静态页、path/timer、单条制作，不发布。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | ---: | ---: | ---: | ---: |
| Python 编译 | 5 | 5 | 0 | 0 |
| Core 单元测试 | 56 | 56 | 0 | 0 |
| Service 单元测试 | 79 | 79 | 0 | 0 |
| Prepare runner 单元测试 | 14 | 14 | 0 | 0 |
| UI/路由契约测试 | 37 | 37 | 0 | 0 |
| GPU/发布链路回归 | 51 | 51 | 0 | 0 |
| TT Post 自动化合计 | 237 | 237 | 0 | 0 |
| 生产关闭态 canary | 1 | 1 | 0 | 0 |

## 执行命令与结果

```powershell
# 结果：通过
python -m py_compile app.py features/tt_posts/core.py features/tt_posts/service.py scripts/tt_post_prepare_runner.py scripts/test_tt_post_prepare_runner.py

# 结果：56/56、79/79、14/14、25/25、12/12、51/51，合计 237/237
python scripts/test_tt_posts_core.py
python scripts/test_tt_posts_service.py
python scripts/test_tt_post_prepare_runner.py
python scripts/test_tt_post_pool_ui.py
python scripts/test_tt_posts_app_contract.py
python scripts/test_tt_gpu_worker.py

# 结果：通过
git diff --check
```

部署后命令以不泄露 secrets 的形式记录：

```text
systemctl is-active tt-post-service.service
systemctl is-active tt-post-prepare.path tt-post-prepare.timer
systemctl is-active tt-post-runner.path tt-post-runner.timer
systemctl list-timers tt-post-prepare.timer tt-post-runner.timer
sqlite3 <db> 'PRAGMA integrity_check;'
sqlite3 <db> '<仅统计 intake/recurring/queue/publish 的脱敏查询>'
```

生产结果：

- 目标 commit SHA：`bb9024ba7b7c7f70112b102e821ba48c21292d3c`
- CPU release：`/opt/tt-post/releases/bb9024ba7b7c`
- 部署前 release：`/opt/tt-post/releases/2055077`
- 备份：`/mnt/data-disk/tt-post-publisher/backups/20260730-185822-2055077-to-bb9024ba7b7c-async-prepare`
- 静态页三处及本机 HTTPS 响应 SHA256：`e53e82314320a5e648255285042093f5c9e354698709b2a86758d546df8cdfcd`
- 迁移副本与生产 `integrity_check`：`ok`
- canary：素材 `5391678`、Drama ID `F59JjB15bc`、隔离 intake `1/queued`；生产原有 recurring pool `1/available` 未改写
- 真实生产 preview：`4.204641s`
- 生产环境隔离账本 preview：`3.105s`
- queued 入池：`4.838s`
- 后台转 ready：未新增真实生产池记录；该素材部署前已是 ready，原子转 ready 由 Core/Service 自动化覆盖
- TikTok init 增量：`0`
- 真实 TikTok Post 增量：`0`

## 缺陷情况

| 缺陷 | 说明 | 状态 |
| --- | --- | --- |
| BUG-001 | 素材校验同步等待 GPU，页面卡在“读取中” | 已修复并部署；生产 preview 4.204641s |

新增缺陷：本地、服务器自动化及关闭态 canary 未发现。

## 验证证据

1. preview 时 Fake GPU `prepare_jobs=[]`、Creator Info 调用为空。
2. 生产环境隔离账本 material-pool POST 返回 queued，替换的远端客户端若被调用会直接令 canary 失败。
3. FIFO、租约过期 reclaim、旧 token completion 被拒均由 Core 自动化覆盖。
4. intake ready 与 recurring pool available 原子一致由 Core/Service 自动化覆盖。
5. `tt-post-prepare.*` 与 `tt-post-runner.*` 同时 active，使用不同 lock/kick。
6. 线上静态文件与本机 HTTPS 响应 SHA256 一致；浏览器未登录，未代用户执行登录或入池。
7. 三个 gate 全 0；部署前后 queue/publish-id/recurring 为 `0|0|1`，部署后 intake 为 0。

## 遗留风险

- 同账号第一条长期 retry_wait 会按严格 FIFO 阻塞后续条目，需要运营监控。
- 本期前端无人工 retry/cancel，failed 需后台诊断。
- 素材池合并列表当前各取 intake/ready 最近 1000 条后再分页，超过该规模需改为数据库级统一分页与汇总。
- 素材 resolver、GPU/COS 网络仍可能慢，但不会再阻塞页面校验请求。

## 发布建议

建议保持当前生产版本。首条由用户实际加入的新素材应自然观察 `queued -> preparing -> ready`，无需再让校验请求等待；真实 TikTok 发布 gate 继续保持关闭。
