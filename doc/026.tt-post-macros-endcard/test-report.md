# 测试报告

## 测试结论

**通过，已部署并完成 prepare-only；没有创建真实 TikTok Post。**

截至 2026-08-03，最终工作区已完成 Python 编译、8 个自动测试脚本和桥接测试；共 312 个 Python 用例及 53 个 Node 断言通过，失败 0。生产 SQLite migration、Nginx TT/X、GPU `direct_outro`、COS 下载、ffprobe、抽帧、外部副作用和隔离回滚均通过。没有创建真实 TikTok Post，也没有保存或人为触发自动排期。

## 测试范围

执行范围包括 `test-cases.md` 的 M/F/U/D/I/S 六组合同、Python 编译检查、最终语义合并评审，以及生产环境的无发布副作用验收。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 | 待执行 |
| --- | ---: | ---: | ---: | ---: | ---: |
| M 宏与 UTF-16 | 10 | 10 | 0 | 0 | 0 |
| F description 冻结与迁移 | 7 | 7 | 0 | 0 | 0 |
| U TT 短链 | 6 | 6 | 0 | 0 | 0 |
| D direct_outro 与媒体 | 8 | 8 | 0 | 0 | 0 |
| I UI 与控制面回归 | 7 | 7 | 0 | 0 | 0 |
| S 安全与回滚 | 4 | 4 | 0 | 0 | 0 |
| 合计 | 42 | 42 | 0 | 0 | 0 |

42 条合同检查由自动化、生产只读比对和 prepare-only 共同覆盖；自动化明细如下。

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
| BUG-001 description 在 intake 后缺少确定性冻结贯穿 | P0 | 修复并验证 | 已关闭 |
| BUG-002 正式直发模式与固定片尾能力分裂 | P0 | 修复并验证 | 已关闭 |

## 验证证据

当前已有证据：

- `requirements.md`：冻结需求和安全边界。
- `sa-review.md`：架构问题与决策。
- `test-cases.md`、`sa-test-review.md`：42/42 验收矩阵及覆盖评审。
- `api-doc.md`、`deploy.md`：接口与计划部署/回滚合同。
- `bugs/BUG-001.md`、`bugs/BUG-002.md`：两个 P0 缺陷记录。
- Python 312/312、Node 53/53、`py_compile`、`compileall` 与 `git diff --check` 均通过。
- 独立边界复核发现并修复多行 description 过早拒绝与历史字面宏队列重放问题；新增回归均通过。

生产证据：

- release commit：`282eb914172531bd55500b65539d5715a282e5bc`；CPU/GPU 均从 GitHub 精确 SHA 构建为只读 release。
- SQLite online backup 完整性 `ok`；migration canary 与 live migration 的 6 张关键表行数保持不变，description/short-link 字段与唯一索引齐全。
- Nginx `nginx -t` 通过；临时 TT 19 位短链本机和公网均 200/`no-store`，既有 X `/s2l/14.html` 仍为 200；临时 wrapper 已删除。
- GPU health：`direct_outro`、`tt-post-direct-outro-hevc-720x1280-v1`、`direct_post_eligible=true`、`phone-match-0.9s`、资产身份 ready。
- prepare-only job：`ttoutro-5801636-20260803-v1`；输出 SHA-256 `19b98f736e8f558af445e21379f0c88e3d49b4861ea410710c5cd6c022daa841`，18,232,895 bytes，134.768 秒，720×1280/30fps，HEVC Main + AAC-LC 48kHz stereo。
- COS 输出：[prepare-only 成片](https://socialkit-cdn.yingliang.tech/tt-post-prepared/19/19b98f736e8f558af445e21379f0c88e3d49b4861ea410710c5cd6c022daa841.mp4)；HEAD 200、Range 206，下载 SHA/size 与 manifest 一致。
- 片尾起始和末段抽帧人工确认 Drama ID、Dramawave Logo、教程提示和 Continue Watching 卡片均存在；临时下载/抽帧/源 URL 文件已删除。
- queue 前后均为 failed=1、published=3，active=0；GPU publish ledger 前后均为 4 且逐文件指纹一致；schedule `account_id=640/version=1/enabled=1/[11:00]` 未变化。
- 三重 gate 均保持开启；19 条账号设置保持 `PUBLIC_TO_EVERYONE` 且 `allow_comment/duet/stitch=1`；runner/timer/path 已恢复 enabled+active，并自然执行成功且未消费任务。
- 隔离回滚：旧 CPU release 可打开 additive migration canary，旧 GPU release 57/57 通过；旧 release/env/Nginx/SQLite/资产/账本备份均存在。

## 遗留风险

- 真实公开 Post 未在本轮创建，因此本报告只证明制作、短链、冻结、门禁和无发布副作用，不把 prepare-only 等同于 TikTok 客户端最终展示验收。
- 旧 queue 若冻结字段为空且 caption 仍含字面宏，必须保持不可发布；本次生产 active/due=0，新队列均走完整冻结链路。

## 发布建议

结论：**本需求已上线，可继续使用现有自动/手动发布入口；本轮没有额外创建测试 Post。** 后续模板请使用精确小写 `{url}` 与 `{desc}`。
