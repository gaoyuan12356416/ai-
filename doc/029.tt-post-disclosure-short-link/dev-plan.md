# 开发计划

| 任务 | 文件 | 状态 |
| --- | --- | --- |
| 新旧短链构建、校验和不可变写入 | `features/tt_posts/links.py` | 已完成 |
| queue ID 冻结与并发断言 | `features/tt_posts/core.py` | 已完成 |
| 自动策略和发布边界归零披露 | `features/tt_posts/service.py`, `core.py` | 已完成 |
| Nginx 新旧路由并存 | `deploy/nginx-tt-short-domain-location.conf` | 已完成 |
| 页面预览和自动化回归 | `static/tt-post-pool.html`, `scripts/test_tt_*` | 已完成 |

## 验证

Python 编译、9 个 TT Python 脚本、Node bridge、页面内联 JavaScript、Nginx 配置合同、`git diff --check`。

## 发布

GitHub-first 不可变 CPU release；备份 SQLite、TT/X 短链目录清单与 hash、Nginx snippet、三份静态页。只重启 TT sidecar并 reload Nginx，不改 GPU。
