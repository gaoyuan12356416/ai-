# 开发计划

## 开发范围

在独立分支实现 TT 描述 `{url}` 宏、X 同款 W2A 参数、`gy.g2flow.com` 跳转页、换行保真测试和部署说明。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 宏解析与队列冻结 | Codex | `features/tt_posts/core.py` | 完成 |
| W2A 与不可变跳转页 | Codex | `features/tt_posts/links.py` | 完成 |
| 发布前短链准备 | Codex | `features/tt_posts/service.py` | 完成 |
| 页面校验与预览 | Codex | `static/tt-post-pool.html` | 完成 |
| 自动化测试与文档 | Codex | `scripts/test_tt_*`、本目录 | 完成 |

## 编译 / 构建命令

```powershell
python -m py_compile features/tt_posts/links.py features/tt_posts/core.py features/tt_posts/service.py
python scripts/test_tt_post_links.py
python scripts/test_tt_posts_core.py
python scripts/test_tt_posts_service.py
python scripts/test_tt_post_pool_ui.py
python scripts/test_tt_gpu_worker.py
```

## 风险与依赖

- 生产依赖 `gy.g2flow.com` Nginx 增加 TT 专用 location。
- `tt-post` 服务账号需要 `/mnt/data-disk/tt-post-publisher/s2l` 写权限。
- TikTok 客户端展示不属于服务端可控范围。

## 完成记录

- 2026-07-31：本地实现和回归完成，未部署、未触发真实发布。
