# 开发计划

## 开发范围

实现一次性可复用的 FB Page 定向历史回补工具及 `create_run` 最小扩展，不改变现有 HTTP 路由、发布器或数据表。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 需求与 SA 冻结 | Codex | `doc/049.fb-auto-targeted-backfill` | 完成 |
| Page 白名单与模板版本锁 | Codex | `features/fb_auto_posts/core.py` | 进行中 |
| validate/apply/指纹/审计脚本 | Codex | `scripts/fb_auto_post_targeted_backfill.py` | 待开发 |
| 单元与安全边界测试 | Codex | `scripts/test_fb_auto_store.py`、新脚本测试 | 待开发 |
| 独立代码/测试评审及全量回归 | Codex | 文档与 FB 测试集 | 待执行 |
| GitHub-first 部署、dry-run、回补、终态核对 | Codex | CPU 生产服务 | 待执行 |

## 编译 / 构建命令

```bash
python -m py_compile features/fb_auto_posts/core.py scripts/fb_auto_post_targeted_backfill.py
python -m unittest scripts.test_fb_auto_store scripts.test_fb_auto_post_targeted_backfill
python -m unittest discover -s scripts -p 'test_fb_auto*.py'
```

## 风险与依赖

- 依赖生产只读 MySQL Page/Token 候选查询、现有指标缓存及 GPU 预制服务。
- apply 是真实发布的上游建单写入，必须先备份 SQLite、校验数据盘挂载并使用 dry-run 指纹。
- 生产建单后需要按自然 timer 观察，不能把 dispatch 当成最终发布成功。

## 完成记录

待实现、测试和生产验收后补充 commit、部署时间、run_id 与最终结果。
