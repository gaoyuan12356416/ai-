# 测试报告

## 测试结论

自动化和生产只读验收均通过：账号设置、动态语言 FIFO、无匹配等待、并发原子领取、手动边界、恢复幂等、页面与代理契约全部通过。生产已部署至 `af95ea73d95b883e591318c7e0ab09cfeb4716e4`；验证全程未创建真实 TikTok Post。

## 测试范围

- 已执行：临时 SQLite 增量迁移、语言合同、账号设置、自动跨池语言 FIFO、并发领取、无匹配等待、后改/恢复、手动边界、随机计划/品牌披露/短链等完整 TT 回归、页面与代理契约。
- 生产已执行：SQLite 在线备份和副本双初始化、索引查询计划、登录态 Chrome 100% 缩放目视、公网页面哈希与服务状态。
- 全部验证禁止真实 TikTok Create Post。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 账号设置页面 | 12 | 12 | 0 | 0 |
| 发布池页面 | 36 | 36 | 0 | 0 |
| 主后台代理契约 | 13 | 13 | 0 | 0 |
| Core | 83 | 83 | 0 | 0 |
| Service | 130 | 130 | 0 | 0 |
| 其他 TT Python 回归 | 98 | 98 | 0 | 0 |
| Python 合计 | 372 | 372 | 0 | 0 |
| Drama bridge 断言 | 53 | 53 | 0 | 0 |
| 语言合同额外定向断言 | 6 | 6 | 0 | 0 |

## 缺陷情况

- BUG-001 已关闭：自动发布不再按入池预分配账号领取，改为按当前账号语言跨全池 FIFO。
- 当前无自动化失败；生产部署验证不作为代码缺陷计入。

## 验证证据

```text
python scripts/test_tt_account_settings_ui.py  -> 12/12 passed
python scripts/test_tt_post_pool_ui.py         -> 36/36 passed
python scripts/test_tt_posts_app_contract.py   -> 13/13 passed
python -m unittest discover -s scripts -p "test_tt*.py" -> 372/372 passed
node scripts/test_tt_drama_bridge.js           -> 53 assertions passed
language contract directed assertions          -> 6/6 passed
python -m py_compile ...                        -> passed
git diff --check                                -> passed
```

- 账号页使用可输入 datalist，默认 `en`，单/批量请求含 `drama_language`，列表显示剧语言。
- 预制作表为 9 列；空语言显示 `en`；未领取显示“等待同剧语言账号领取”。
- 代理单条/批量审计白名单包含 `drama_language`。
- 单素材并发只有一个账号领取，另一账号得到 `tt_post_recurring_pool_language_empty`；双素材并发领取不同素材。
- 三条同语言素材跨原预制作账号严格按 `created_at,id` 领取。
- active manual canary 对应素材会从其他账号的自动领取候选中排除；手动 readiness 只看精确账号素材。
- 素材池会分批加载全部记录再按实际领取账号合并筛选，发布状态也按每批最多 1000 个素材聚合；超过 1000 条时分页总数仍准确。
- 旧库会回填持久化 `routing_language` 并建立 `(status,routing_language,created_at,id)` 索引；非法历史语言被隔离，不能阻断合法 FIFO。
- 未触发真实账号保存、真实 GPU 发布或 TikTok Direct Post。

## 遗留风险

- 当前生产账号和素材语言均规范为 `en`；非 `en` 的真实自然发布要由后续业务数据验证，但不得为验收造任务。
- 自动 claim 已使用持久规范列和复合 FIFO 索引，生产副本与活动库的 `EXPLAIN QUERY PLAN` 均确认命中。

## 发布建议

生产发布与只读验收完成，保留 online backup、旧 release 和精确页面回滚点。后续观察自然调度即可；不得为了证明语言路由而手工创建真实 TikTok Post。
