# 测试报告

## 测试结论

本地自动化回归已通过，生产关闭态 canary 尚待部署后执行。当前结论为“代码可进入生产关闭态验证”，不把本地通过等同于生产验收完成。

发布门槛：

- 全部 P0/P1 自动化通过；
- SQLite migration 在备份副本与生产均 `integrity_check=ok`；
- 生产 preview -> queued -> ready canary 成功；
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
| 生产关闭态 canary | 1 | 待填 | 待填 | 待填 |

## 待执行命令与结果

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

结果占位：

- 目标 commit SHA：待填
- CPU release：待填
- 部署前 release/备份：待填
- 静态页三处 SHA256：待填
- `integrity_check`：待填
- canary material/intake/recurring pool ID：待填
- preview 响应耗时：待填
- queued 入池响应耗时：待填
- 后台转 ready 总耗时：待填
- TikTok init 增量：待填（验收要求 0）
- 真实 TikTok Post 增量：待填（验收要求 0）

## 缺陷情况

| 缺陷 | 说明 | 状态 |
| --- | --- | --- |
| BUG-001 | 素材校验同步等待 GPU，页面卡在“读取中” | 本地 237/237 通过，待生产关闭态 canary |

新增缺陷：本地自动化未发现。

## 验证证据

待附：

1. preview 时 Fake GPU `prepare_jobs=[]` 的测试断言。
2. material-pool POST 返回 queued 后 runner 才调用 prepare 的测试断言。
3. FIFO、租约过期 reclaim、旧 token completion 被拒的测试结果。
4. intake ready 与 recurring pool available 原子一致的 SQLite 查询。
5. path/timer 与 publish runner 相互独立的 systemd 状态。
6. 浏览器状态表 queued -> preparing -> ready 截图。
7. gate 全 0、无 TikTok publish ID/真实帖子增量的只读查询。

## 遗留风险

- 同账号第一条长期 retry_wait 会按严格 FIFO 阻塞后续条目，需要运营监控。
- 本期前端无人工 retry/cancel，failed 需后台诊断。
- 素材池合并列表当前各取 intake/ready 最近 1000 条后再分页，超过该规模需改为数据库级统一分页与汇总。
- 素材 resolver、GPU/COS 网络仍可能慢，但不会再阻塞页面校验请求。

## 发布建议

本地结论为“可部署并执行生产关闭态 canary”，尚不能宣告生产验收完成；即使 canary 通过，也必须保持真实 TikTok 发布 gate 关闭。
