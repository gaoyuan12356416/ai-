# 开发计划

## 开发范围

在生产 commit `d3202fc829379fce91de6ffa4588cd29af36492e` 上增量实现模板枚举、
执行路由、双 GPU 部署单元、页面与测试；不修改 TT 发布池业务语义。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 严格枚举与历史默认 | Codex | `validation.py` | 已完成 |
| 冻结版本执行路由 | Codex | `publisher.py`, `service.py` | 已完成 |
| 编辑页选择与摘要 | Codex | TT auto HTML/JS | 已完成 |
| 双 GPU systemd/env 合同 | Codex | `deploy/`, `.env.example` | 已完成 |
| 自动化与静态合同测试 | Codex | `scripts/test_tt_auto_*` | 已完成 |
| GitHub-first 生产部署与浏览器验收 | Codex | CPU/GPU/Chrome | 已完成 |

## 编译 / 构建命令

```bash
python -m py_compile features/tt_auto_posts/validation.py features/tt_auto_posts/publisher.py features/tt_auto_posts/service.py features/tt_posts/service.py
node --check static/tt-auto-publish-template.js
python -m unittest scripts.test_tt_auto_post_store scripts.test_tt_auto_post_service scripts.test_tt_auto_post_selector scripts.test_tt_auto_post_runner scripts.test_tt_auto_post_publisher scripts.test_tt_auto_post_metrics scripts.test_tt_auto_post_links scripts.test_tt_auto_code_broker scripts.test_tt_auto_publish_ui scripts.test_tt_auto_publish_app_contract
python scripts/test_tt_gpu_worker.py
python scripts/test_tt_posts_service.py
git diff --check
```

## 风险与依赖

- 依赖现有经批准片尾资产 SHA 与 GPU v2 direct-outro 代码。
- 依赖 GPU 到 CPU 的独立反向隧道；部署前后不能中断现有 18830 通道。
- 发布前必须确认 TT auto in-flight 为 0，并保留 SQLite 在线备份与现有 release/env/unit。

## 完成记录

- 2026-08-12：完成生产现状、端口、资产 SHA 和历史代码只读核对。
- 2026-08-12：完成实现与 346 项离线回归，随后进入生产部署与真实浏览器只读验收。
- 2026-08-12：GitHub commit `18559c03cc68afe83af87b963bf812e09320bb3a` 已部署；
  双路由 health、自然 scheduler/runner、数据库事实对比与登录态浏览器只读验收通过。
