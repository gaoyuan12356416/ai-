# 测试报告

## 测试结论

本地与服务器候选全量通过，生产已部署并完成只读验收。自动化全部使用临时数据库和 fake 外部依赖，没有连接生产 TikTok 或创建 Post。

## 执行统计

| 脚本 | 结果 |
| --- | ---: |
| `test_tt_account_settings_ui.py` | 11/11 |
| `test_tt_gpu_worker.py` | 67/67 |
| `test_tt_post_direct_config_core.py` | 8/8 |
| `test_tt_post_links.py` | 6/6 |
| `test_tt_post_pool_ui.py` | 34/34 |
| `test_tt_post_prepare_runner.py` | 16/16 |
| `test_tt_posts_app_contract.py` | 13/13 |
| `test_tt_posts_core.py` | 68/68 |
| `test_tt_posts_service.py` | 121/121 |
| **Python 合计** | **344/344** |

Node `test_tt_drama_bridge.js` 为 53/53 断言通过；目标 Python 文件编译、页面内联 JavaScript 语法和 `git diff --check` 均通过。

## 专项证据

- 启用多账号配置后 Creator Info 调用为 0。
- 不传 `source_account_id` 的入池请求稳定分配到已保存账号；幂等重试返回同一 intake/账号。
- 入池与预制作期间账号查询和 Creator Info 被 mock 为抛错，任务仍完成为 `ready`。
- 已保存账号为空时以 `tt_post_auto_accounts_required` 原子拒绝。
- 700 秒素材可预制作完成；真正发布时仍由实时 600 秒账号上限阻断并释放 FIFO 素材。
- 立即测试仍要求账号、设置、Creator Info 和最大时长校验。

## 缺陷

无开放 P0/P1。开发中发现的立即测试边界和非 ASCII 比较问题均已修复并纳入回归。

## 遗留风险

账号最大时长不再在后台预制作阶段提前判断，可能在真正发布时才暴露账号时长不匹配；系统会保留可审计失败且不会初始化不合规发布。

## 发布建议

已按 GitHub-first 部署 CPU sidecar 和三份静态页；未改数据库 schema 或 GPU。生产只读验收未创建素材或 Post，结论通过。
