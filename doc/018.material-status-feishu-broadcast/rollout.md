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

## 十字段增量发布

### 发布结果

2026-07-28 17:32（Asia/Shanghai）完成十字段增量发布。新增
`resource_name`（资源名）和 `drama_dubbing_type`（剧集配音类型），保留既有
`task_type`（任务类型）；三项均进入私聊与兜底播报。Token、来源 IP 策略、
映射链、Nginx 和数据库结构均未改变。

### 精确版本

- 分支：`codex/material-status-add-fields-20260728`
- 提交：`8af21dbead5fd6fcf5f048319d76971573def77c`
- 上一生产代码基线：`f1d49213c7a15632c92acfa9b2417493c24c3deb`
- 生产 `app.py` SHA-256（未变）：`43a0f842413feaa7c51dffc7226092519cb795cf8a7b7c805ae6dd7d9852c9b3`
- 新 `service.py` SHA-256：`ac903f876a1dde94287badd935d03512c910bb65b80fd32a312a57d32ede2c35`
- 新公开文档 SHA-256：`75a28bf49f96a33870b2eee45ca0ba5b5f569b3a5f095c94d7a7b3e90dbd4ca3`
- 发布目录：`/root/releases/drama-material-service-8af21dbe-material-status-10fields`

### 发布范围

- 替换 `features/material_status_broadcast/service.py`
- 替换 `scripts/test_material_status_broadcast.py`
- 替换 `scripts/test_material_status_webhook_app.py`
- 更新 `/usr/share/nginx/html/docs/material-status-api/index.html`
- 仅重启 `drama-material-api.service`
- 未修改 `app.py`、`.env`、Nginx、MySQL 或 SQLite 表结构

### 验收证据

- 生产切换前 outbox：`delivered=2`，`queued/retry/processing=0`
- 本地：139/139
- 精确发布目录：专项 28/28；相关回归及 FB playable 共 138 项通过
- 旧八字段：`422 invalid_payload`，outbox 记录数 0
- 新十字段：`202 accepted`，事件 `MSE-0000000003`
- 投递：`delivered/fallback/attempt1`，目标群
  `oc_88f2eb329508d13bfd2be3de0e221797`
- 幂等重放：返回同一事件，该 key 的 outbox 记录数仍为 1
- 飞书稳定 UUID 重放：返回同一
  `om_x100b69b339fe58a0ddaaaec9e33cd03`，chat_id 匹配，响应正文与实际播报
  完全一致，十字段顺序正确且各出现一次
- 公网错误 Token：`401`
- 公开文档：`200`，公网 SHA-256 与发布文件一致
- 服务：17:32:04 起 active，发布后 warning/error journal 为 0
- Token：未轮换，数量 1，指纹前缀仍为 `ecd760a9d90b8399`
- Token 泄漏扫描：journal、Nginx 日志、SQLite、公开文档、源码均为 0 命中

生产机默认 Python 3.9；无关的 `test_playable_preview_docs.py` 主动要求
Python 3.10+，因此该单项只在本地同一提交通过，不作为本字段增量的生产阻断项。
真实私聊 canary 仍因未指定安全测试账号而未执行；私聊路径已由自动化验证三项
字段与十字段顺序。

### 回滚点

- 目录：`/mnt/data-disk/backups/drama-material-service/material-status-10fields-20260728T172922-pre-8af21dbe`
- 文件清单：当前 app、`.env`、完整 feature、两份测试、Nginx、公开文档
- SQLite：在线备份，`PRAGMA quick_check=ok`
- `SHA256SUMS`：全部通过

如需回滚，恢复备份中的 feature、两份测试和公开文档后只重启
`drama-material-api.service`；保留十字段 canary 事件，不删除审计数据。
