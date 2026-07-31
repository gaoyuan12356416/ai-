# 开发计划

## 开发范围

为 TT Post CPU/GPU 发布链路增加默认关闭的一次性人工私密发布白名单，并更新管理页状态。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 配置与目标模型 | DEV | `features/tt_posts/service.py`、`features/tt_gpu/worker.py` | 进行中 |
| 队列人工运行反查 | DEV | `features/tt_posts/core.py` | 进行中 |
| CPU 人工路径与强制策略 | DEV | `features/tt_posts/service.py` | 进行中 |
| GPU 二次校验与幂等调用 | DEV | `features/tt_gpu/worker.py` | 进行中 |
| 管理页按钮与提示 | DEV | `static/tt-post-pool.html` | 进行中 |
| 自动化回归 | QA | `scripts/test_tt_posts_*.py`、`scripts/test_tt_gpu_worker.py` | 待执行 |
| GitHub-first CPU/GPU 部署 | OPS | 两台生产服务器 | 待执行 |

## 编译 / 验证命令

```text
python -m py_compile features/tt_posts/core.py features/tt_posts/service.py features/tt_gpu/worker.py
python -m unittest scripts.test_tt_posts_core scripts.test_tt_posts_service scripts.test_tt_gpu_worker scripts.test_tt_post_pool_ui
git diff --check
```

## 风险与依赖

- 依赖 GPU 已存在目标成片 manifest。
- 依赖账号 token 与实时 creator info 仍有效。
- TikTok 是否接受未审核/未验证来源只能由真实测试结果确认。
- 部署必须保持正式门禁和每日排期关闭。

## 完成记录

待开发、测试与部署后填写。
