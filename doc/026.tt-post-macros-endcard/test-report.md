# 测试报告

## 测试结论

**离线自动测试通过；生产部署与 prepare-only 证据待回填。**

截至 2026-08-03，最终工作区已完成 Python 编译、8 个自动测试脚本和桥接测试；共 312 个 Python 用例及 53 个 Node 断言通过，失败 0。尚未在本文回填生产 migration、Nginx 与 GPU prepare-only 结果。没有创建真实 TikTok Post，也没有保存或人为触发自动排期。

## 测试范围

执行范围包括 `test-cases.md` 的 M/F/U/D/I/S 六组合同、Python 编译检查、最终语义合并评审，以及生产环境的无发布副作用验收。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 | 待执行 |
| --- | ---: | ---: | ---: | ---: | ---: |
| M 宏与 UTF-16 | 10 | 0 | 0 | 0 | 10 |
| F description 冻结与迁移 | 7 | 0 | 0 | 0 | 7 |
| U TT 短链 | 6 | 0 | 0 | 0 | 6 |
| D direct_outro 与媒体 | 8 | 0 | 0 | 0 | 8 |
| I UI 与控制面回归 | 7 | 0 | 0 | 0 | 7 |
| S 安全与回滚 | 4 | 0 | 0 | 0 | 4 |
| 合计 | 42 | 0 | 0 | 0 | 42 |

上表将在生产验收后按实际证据逐项回填；当前已通过的自动化明细如下。

| 自动化脚本 | 结果 |
| --- | ---: |
| `test_tt_post_links.py` | 5/5 |
| `test_tt_posts_core.py` | 68/68 |
| `test_tt_posts_service.py` | 103/103 |
| `test_tt_post_pool_ui.py` | 32/32 |
| `test_tt_post_prepare_runner.py` | 14/14 |
| `test_tt_gpu_worker.py` | 67/67 |
| `test_tt_posts_app_contract.py` | 12/12 |
| `test_tt_account_settings_ui.py` | 11/11 |
| `test_tt_drama_bridge.js` | 53/53 assertions |

## 缺陷情况

| 缺陷 | 严重级别 | 当前状态 | 发布影响 |
| --- | --- | --- | --- |
| BUG-001 description 在 intake 后缺少确定性冻结贯穿 | P0 | 代码修复与自动回归通过，生产迁移待验收 | 环境验收中 |
| BUG-002 正式直发模式与固定片尾能力分裂 | P0 | `direct_outro` 代码与 67 个 GPU 回归通过，prepare-only 待验收 | 环境验收中 |

## 验证证据

当前已有证据：

- `requirements.md`：冻结需求和安全边界。
- `sa-review.md`：架构问题与决策。
- `test-cases.md`、`sa-test-review.md`：42 条待执行矩阵及覆盖评审。
- `api-doc.md`、`deploy.md`：接口与计划部署/回滚合同。
- `bugs/BUG-001.md`、`bugs/BUG-002.md`：两个 P0 缺陷记录。
- Python 312/312、Node 53/53、`py_compile`、`compileall` 与 `git diff --check` 均通过。
- 独立边界复核发现并修复多行 description 过早拒绝与历史字面宏队列重放问题；新增回归均通过。

尚缺的真实证据：

- migration 前后 schema/数据对比。
- Nginx TT/X 无缓存请求。
- GPU health、prepare 请求/响应、manifest、ffprobe、SHA/size、抽帧与人工观看。
- queue/publish ledger/真实 Post/schedule 前后基线。
- CPU/GPU 隔离回滚演练。

## 遗留风险

- `direct_outro` 尚未通过生产固定片尾资产和 COS 成片的 prepare-only 验证。
- description 老数据回填/阻断策略尚未在数据库副本演练。
- 可变 source URL 与 prepare reuse 的身份风险尚需用 source SHA/size 或不可变 URL 关闭。
- TT 短链 Nginx 优先级与 X 链接兼容性尚未环境验证。

## 发布建议

当前建议：**允许按已授权的 GitHub-first 变更窗部署并执行 prepare-only；禁止创建真实 Post。** 若 migration、Nginx、GPU health、资产指纹或外部状态比对任一失败，立即按双轨回滚方案停止上线。
