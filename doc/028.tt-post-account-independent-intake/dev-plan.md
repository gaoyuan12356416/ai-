# 开发计划

## 开发范围

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| 配置保存去 Creator Info 依赖 | `features/tt_posts/service.py` | 已完成 |
| 入池自动稳定分配账号 | `features/tt_posts/service.py` | 已完成 |
| 预制作去账号能力依赖 | `features/tt_posts/service.py` | 已完成 |
| 页面去入池账号选择门禁 | `static/tt-post-pool.html` | 已完成 |
| 服务/UI 回归测试 | `scripts/test_tt_posts_service.py`, `scripts/test_tt_post_pool_ui.py` | 已完成 |

## 验证

- Python 编译检查。
- 9 个 TT Post Python 测试脚本。
- Node bridge 测试（如仓库存在）。
- 页面内联 JavaScript 语法检查。
- `git diff --check`。

## 发布

GitHub-first：提交、推送并核对远端 commit 后，在 CPU 主机创建不可变 release；备份 SQLite 和三份静态页，只重启受影响的 TT Post sidecar，不改 GPU。

## 风险与依赖

- 生产数据库无 schema 变化。
- 页面和 sidecar 需同版本部署，否则旧页面仍会强制选择账号。
- 不调用真实发布接口进行验收。
