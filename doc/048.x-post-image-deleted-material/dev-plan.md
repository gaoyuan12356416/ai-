# 开发计划

## 开发范围

Selector、共享媒体服务、素材预检 runner、相关测试、需求与部署证据。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 精确路径规则 | Codex | `features/x_posts/selector.py` | 已完成 |
| 图片下载/探测/上传 | Codex | `features/x_posts/service.py` | 已完成 |
| 图片预检 | Codex | `scripts/x_post_daily_runner.py` | 已完成 |
| 单元与回归 | Codex | `scripts/test_x_*.py` | 已完成 |
| GitHub/生产部署 | Codex | release + main overlay | 待处理 |

## 编译 / 构建命令

```bash
python -m py_compile features/x_posts/selector.py features/x_posts/service.py scripts/x_post_daily_runner.py scripts/x_post_schedule_runner.py scripts/x_post_manual_runner.py
python scripts/test_x_post_material_pool_selector.py
python scripts/test_x_posts.py
python scripts/test_x_post_daily.py
python scripts/test_x_post_material_pool.py
python scripts/test_x_post_manual_runner.py
python scripts/test_x_post_material_random_relay.py
git diff --check
```

## 风险与依赖

- X 官方 `tweet_image`/`tweet_gif` 规格；验证只使用 mock，不发真实 Post。
- 生产 main selector 与 Sidecar release 当前哈希不一致，部署必须统一为本次 Git 提交。

## 完成记录

- 图片和软删除视频仅在 pool/manual selector 开启，X Auto 默认参数保持严格旧规则。
- 历史通用错误只允许进入重检扫描，不直接显示可用；本轮 selector 成功后清空，失败后写精确错误。
- 图片下载、ffprobe 解码、上传类别及最终 mock Post 链路已贯通。
- 本地完整 X 回归执行 436 项：435 通过，1 项 Windows symlink 环境跳过。
