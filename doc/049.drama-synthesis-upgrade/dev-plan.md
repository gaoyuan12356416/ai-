# 开发计划

## 开发范围与基线

- 分支：`codex/drama-synthesis-upgrade-20260826`
- 基线：`6f8bdf0`；CPU 线上 `app.py` blob 已确认与该提交一致。
- 工作树：独立 clean worktree，不修改 `D:\codex\ai-drama-material-service` 脏 checkout 或预览 worktree。
- 线上 static 无 Git 提交；候选已纳入线上唯一已知差异：需求审核展示完整 `demand_text`。

## 任务拆分

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| 不可变配方、短链、YouTube ledger | `features/drama_synthesis/core.py` | 完成 |
| HK GPU catalog/render adapter | `features/drama_synthesis/gpu.py`、`app.py` | 完成 |
| YouTube server-only repository/client/engine | `features/drama_synthesis/youtube.py` | 完成 |
| API、任务状态、审计接入 | `app.py` | 完成 |
| UI 保持现有视觉并增加交互 | `static/index.html` | 完成 |
| 异步 worker/systemd/HK 隧道样例 | `scripts/`、`deploy/` | 完成 |
| focused offline tests | `scripts/test_drama_synthesis_upgrade.py` | 完成 |
| 独立 SA/QA 评审 | review/test 文档 | 待执行 |

## 构建与定向验证

```bash
python -m py_compile app.py features/drama_synthesis/core.py features/drama_synthesis/gpu.py features/drama_synthesis/youtube.py scripts/drama_youtube_publish_worker.py scripts/test_drama_synthesis_upgrade.py
python scripts/test_drama_synthesis_upgrade.py
node --check <从 static/index.html 提取的内联脚本>
git diff --check
```

## 发布依赖

1. 独立 QA/SA 通过并由 CEO 明确授权。
2. 候选提交先推 GitHub，再由 CPU/HK 从同一提交部署。
3. HK 完成 20 文件和总字节清单核验，8788 health/render canary 通过。
4. 新 18788 隧道通过后才修改 CPU `GPU_VIDEO_WORKER_URL`；保留 18787 回滚。
5. 短链发布器和 YouTube source hostname allowlist 有明确值；否则相应功能保持不可用。

## 完成记录

- 实现由 implementation owner 完成；未执行生产写入、服务重启、Git push 或真实外部发布。
