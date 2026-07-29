# 开发计划

## 开发范围

在现有 AI 平台新增 TT Post 发布池，CPU 负责业务面，GPU 负责数据盘成片和 TikTok API 面。真实 Direct Post 以三重环境门禁默认关闭。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 需求、规则和 API 设计 | PM/SA | `doc/018.tt-post-publish-pool/` | 完成 |
| CPU SQLite 状态机与安全账号源 | 开发 | `features/tt_posts/` | 进行中 |
| GPU 成片与 TikTok sidecar | 开发 | `features/tt_gpu/`、`scripts/tt_gpu_worker.py` | 进行中 |
| 主后台路由、权限和审计 | 开发 | `app.py` | 待开始 |
| 发布池 UI 与导航 | 前端 | `static/tt-post-pool.html`、导航配置 | 进行中 |
| claim/runner、systemd 与隧道 | 开发 | `scripts/`、`deploy/` | 待开始 |
| 单元、合同、浏览器与线上关闭态验收 | QA/SA | `scripts/test_tt_*.py` | 待开始 |
| GitHub-first CPU/GPU 部署 | 运维 | 两台服务器 immutable release | 待开始 |

## 编译 / 构建命令

```powershell
python -m compileall features scripts
python -m unittest discover -s scripts -p "test_tt_*.py" -v
```

浏览器验收使用线上 Cookie 会话打开 `/tt-post-pool.html`，验证权限、账号、素材预览、表单门禁和任务列表；不提交真实 TikTok Post。

## 风险与依赖

- 生产主服务与 X sidecar 来自两个已部署分支，需在整合分支中无损合并。
- 依赖 CPU 的 63350 只读数据库连接和 GPU 的 `/data`、NVENC FFmpeg。
- 依赖 CPU↔GPU 的专用 SSH 反向隧道。
- TikTok Direct Post 的审核、Intended Use、品牌片尾和 URL Property 未确认，live gate 必须保持关闭。
- 快照 Token 无 scope 元数据，必须以 `creator_info` 实测为准。

## 完成记录

- 2026-07-29：完成线上 X/TT 快照和 GPU 数据盘只读核验。
- 2026-07-29：确定 CPU/GPU 业务边界、数据结构、状态机和合规门禁。
