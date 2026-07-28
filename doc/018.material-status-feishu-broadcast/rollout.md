# 生产发布记录

## 发布结果

2026-07-28 15:52（Asia/Shanghai）完成发布，接口、后台 worker、飞书兜底和公开文档均正常。

## 精确版本

- 分支：`codex/material-status-feishu-webhook-20260728`
- 功能提交：`1498f6d236096242ae96ea7d58592e07848909c2`
- 生产合并提交：`f1d49213c7a15632c92acfa9b2417493c24c3deb`
- 部署时线上并发基线：`c254f388f4b876cf2f8c42507b352780d48b8930`
- 生产 `app.py` SHA-256：`43a0f842413feaa7c51dffc7226092519cb795cf8a7b7c805ae6dd7d9852c9b3`
- 发布目录：`/root/releases/drama-material-service-f1d49213`

## 配置

- 鉴权：独立 Bearer Token
- IP 白名单：无
- 兜底群：`oc_88f2eb329508d13bfd2be3de0e221797`
- Token 长度：64
- Token SHA-256 前缀：`ecd760a9d90b8399`
- Token 只保存在生产服务器 `.env`，未进入 Git、文档、日志、SQLite 或公开页面

## 验收证据

- 生产专项测试：28/28
- 合并基线回归：通过
- MySQL：只读，精确 username → email 成功
- 飞书：email → open_id 成功
- 公网错误 Token：401
- 公网超限请求：JSON 413
- 公开文档：200
- 兜底 canary：`MSE-0000000001`，`delivered/fallback/attempt1`
- 相同幂等键重放：事件编号不变，outbox 记录数为 1
- 飞书消息回读：指定兜底群和测试资源 ID 均匹配
- 发布后服务错误日志：0

未指定安全私聊测试账号，因此未向任意员工发送真实私聊 canary；私聊代码自动化和真实只读用户解析链均已验证。

## 回滚点

- 目录：`/mnt/data-disk/backups/drama-material-service/material-status-20260728T1548-pre-f1d49213`
- 备份前 `app.py` SHA-256：`22b4eaab93bf3690fa4de87f1bb2d2d27aff39bcf0139f24da2d6146d5d43973`
- SQLite 在线备份：36,995,072 bytes
- `SHA256SUMS`：全部通过
- SQLite `PRAGMA quick_check`：`ok`
- 发布记录 SHA-256：`ba5ddb5f05496df05c2f50d884ee1c51cf2ed7aa5483efe96e6acfefdfff4db0`

如需回滚：先停止新接口的 Nginx exact location，再恢复备份的 `app.py` 和 `.env`；删除本次新增且备份标记为原先不存在的 feature、Nginx 配置和公开文档；重启 API、校验既有 `/api/auth/status` 后再 reload Nginx。新增 outbox 表保留，不删除审计事件。
