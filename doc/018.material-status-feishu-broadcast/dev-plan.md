# 开发计划

## 开发范围

实现素材任务状态 Webhook、持久化 outbox、优化师/飞书匹配、私聊与兜底群播报，并完成接口文档和生产部署。

2026-07-28 十字段增量：新增 `resource_name`、`drama_dubbing_type`，
保留 `task_type`，三项进入私聊和兜底消息；不修改 Token、Nginx、SQLite
表结构或优化师匹配链。

## 任务拆分

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| 输入规范化、幂等和 outbox | `features/material_status_broadcast/` | 已完成 |
| Token 鉴权和 HTTP 路由 | `app.py` | 已完成 |
| MySQL 与飞书适配器 | `app.py` | 已完成 |
| 后台投递 worker | `app.py` | 已完成 |
| Nginx 精确反向代理 | `deploy/nginx/material-status-webhook.conf` | 已完成 |
| 单元和编排测试 | `scripts/test_material_status_*.py` | 已完成 |
| 对外接口文档和公开 HTML | `doc/018.material-status-feishu-broadcast/` | 已完成 |
| GitHub-first 部署和生产验收 | CPU 服务器 | 已完成 |

## 编译与测试命令

```powershell
python -m py_compile app.py features\material_status_broadcast\service.py
python scripts\test_material_status_broadcast.py
python scripts\test_material_status_webhook_app.py
git diff --check
```

生产侧还需执行：

```bash
python3 -m py_compile app.py features/material_status_broadcast/service.py
nginx -t
systemctl restart drama-material-api.service
systemctl status drama-material-api.service --no-pager
```

## 风险与依赖

- 线上目录不是 Git 工作树，必须从与线上 `app.py` SHA-256 完全一致的 GitHub 提交构建发布包，只安装本需求涉及的文件。
- `.env` 中 Token 属于服务器密钥，只能原子更新并保留 `0600` 权限。
- 兜底群真实发送会产生一条可见测试消息，生产验收时必须标注“联调测试”。
- 未指定安全的私聊测试用户时，不向任意员工发送测试消息。

## 完成记录

- 功能提交：`1498f6d236096242ae96ea7d58592e07848909c2`
- 生产合并提交：`f1d49213c7a15632c92acfa9b2417493c24c3deb`
- 合并的并发线上基线：`c254f388f4b876cf2f8c42507b352780d48b8930`
- 部署时间：2026-07-28 15:52（Asia/Shanghai）
- 生产发布目录：`/root/releases/drama-material-service-f1d49213`
- 回滚点：`/mnt/data-disk/backups/drama-material-service/material-status-20260728T1548-pre-f1d49213`
- 兜底验收事件：`MSE-0000000001`，送达、幂等重放和飞书回读均通过

## 十字段增量记录

- 分支：`codex/material-status-add-fields-20260728`
- 精确提交：`8af21dbead5fd6fcf5f048319d76971573def77c`
- 生产切换前 outbox：`delivered=2`，`queued/retry/processing=0`
- 本地专项：28/28；相关回归：111/111；总计：139/139
- Python 3.9 grammar、HTML 解析、`git diff --check`：通过
- 部署时间：2026-07-28 17:32（Asia/Shanghai）
- 发布目录：`/root/releases/drama-material-service-8af21dbe-material-status-10fields`
- 回滚点：`/mnt/data-disk/backups/drama-material-service/material-status-10fields-20260728T172922-pre-8af21dbe`
- 十字段 canary：`MSE-0000000003`，兜底一次送达、幂等重放和飞书响应正文核对通过
