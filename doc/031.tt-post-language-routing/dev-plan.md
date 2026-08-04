# 开发计划

## 开发范围

账号剧语言设置、增量迁移、自动发布语言 FIFO、后台预制作状态展示、代理审计、自动化测试和 GitHub-first 部署。立即发布测试、TikTok 网络协议和 GPU 制作协议保持不变。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 固化需求、异常语义和 SA 决策 | PM/SA | `doc/031.tt-post-language-routing` | 已完成 |
| 增加语言规范化与账号设置列 | Codex | `features/tt_posts/core.py` | 已完成 |
| 改造自动语言 FIFO 和手动边界 | Codex | `features/tt_posts/core.py`、`features/tt_posts/service.py` | 已完成 |
| 代理审计透传剧语言 | Codex | `app.py` | 已完成 |
| 个号管理剧语言输入/回填/批量保存 | Codex | `static/tt-account-settings.html` | 已完成 |
| 预制作状态剧语言和领取账号展示 | Codex | `static/tt-post-pool.html` | 已完成 |
| Core/Service/代理/页面回归 | QA/Codex | `scripts/test_tt_*.py` | 已完成 |
| 生产副本迁移和只读验收 | QA/Ops | 部署主机、SQLite 副本、公网页面 | 待执行 |

## 编译 / 构建命令

```powershell
python -m py_compile app.py features/tt_posts/core.py features/tt_posts/links.py features/tt_posts/service.py scripts/tt_post_prepare_runner.py
python -m unittest scripts.test_tt_account_settings_ui scripts.test_tt_gpu_worker scripts.test_tt_posts_app_contract scripts.test_tt_posts_core scripts.test_tt_posts_service scripts.test_tt_post_direct_config_core scripts.test_tt_post_links scripts.test_tt_post_pool_ui scripts.test_tt_post_prepare_runner
node scripts/test_tt_drama_bridge.js
git diff --check
```

定向快速检查：

```powershell
python scripts/test_tt_account_settings_ui.py
python scripts/test_tt_post_pool_ui.py
python -m unittest scripts.test_tt_posts_app_contract scripts.test_tt_posts_core scripts.test_tt_posts_service
```

## 风险与依赖

- 依赖现有 `material_language` 已由 Dramawave 校验器确认；本需求不从用户请求信任素材语言。
- 生产 SQLite 必须在数据盘完成在线备份和副本迁移演练后再重启 sidecar。
- 自动领取 SQL 需在生产副本量级检查延迟和查询计划。
- 测试不得调用真实 Creator Token、GPU 发布或 TikTok Direct Post。
- 多 agent 共用工作树，提交前必须确认仅包含本需求文件且不覆盖他人修改。

## 完成记录

- 2026-08-04：UI 定向测试 `test_tt_account_settings_ui.py` 12/12、`test_tt_post_pool_ui.py` 36/36，`git diff --check` 通过。
- 2026-08-04：账号语言规范化边界复核发现 casefold 后可能扩长；实现已改为对规范值校验长度，超长统一 `invalid_drama_language`，并补 32 个 `ß` 的回归。
- 2026-08-04：补齐“两个账号竞争一条素材”和“三条同语言素材跨原预制作账号 FIFO”的 P0 回归。
- 2026-08-04：修复 active canary 被自动任务抢占、手动 readiness 误判、全池扫描和非法历史语言阻塞；增加持久规范路由键及复合索引。
- 2026-08-04：补齐超过 1000 条的素材池最终账号筛选，以及 active canary + 已启用排期的按钮/执行一致性。
- 2026-08-04：Python 完整 TT 回归 372/372 通过（Core 83、Service 130）；Drama bridge 53 项断言通过；py_compile 与 `git diff --check` 通过。
- 2026-08-04：候选机旧 SQLite 不支持测试用 `DROP COLUMN`；生产迁移未受影响，测试已改为兼容式重建旧表并完成本地 372/372 复验，详见 `bugs/BUG-002.md`。
- 生产副本迁移、浏览器登录态只读验收和部署证据仍须在正式部署窗口回填；不得用真实发布验证。
