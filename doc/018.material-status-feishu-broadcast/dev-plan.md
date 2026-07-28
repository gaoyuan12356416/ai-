# 开发计划

## 开发范围

实现素材任务状态 Webhook、持久化 outbox、优化师/飞书匹配、私聊与兜底群播报，并完成接口文档和生产部署。

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
| GitHub-first 部署和生产验收 | CPU 服务器 | 待执行 |

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

完成后在本文件记录提交、部署时间、生产版本和回滚点。
