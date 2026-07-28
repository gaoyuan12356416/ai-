# 部署与回滚

## 变更内容

- 新增素材任务状态接收接口和独立 Bearer Token
- 新增 SQLite 幂等 outbox 和后台投递 worker
- 新增优化师 MySQL 映射、飞书 open_id 解析、私聊和兜底群播报
- 新增 Nginx 精确反向代理
- 新增公开接口文档
- 十字段增量新增 `resource_name` 和 `drama_dubbing_type`，保留既有
  `task_type`，三项统一进入私聊与兜底播报

## 配置项

真实值只保存在服务器 `/root/drama_material_service/.env`，文件权限保持 `0600`。

```dotenv
MATERIAL_STATUS_WEBHOOK_TOKENS=
MATERIAL_STATUS_WEBHOOK_FALLBACK_CHAT_ID=oc_88f2eb329508d13bfd2be3de0e221797
MATERIAL_STATUS_WEBHOOK_MAX_BODY_BYTES=32768
MATERIAL_STATUS_WEBHOOK_MAX_ATTEMPTS=5
MATERIAL_STATUS_WEBHOOK_POLL_SECONDS=1
MATERIAL_STATUS_WEBHOOK_LEASE_SECONDS=300
```

- `MATERIAL_STATUS_WEBHOOK_TOKENS` 支持逗号分隔的新旧 Token，以便无停机轮换。
- 每个有效 Token 至少 32 个字符。
- 不配置 Token 时接口必须 fail closed，并返回服务未配置。
- 不配置来源 IP 白名单。

## 数据库变更

- 不修改 MySQL，不执行 MySQL DDL 或写入。
- 在现有任务 SQLite 中幂等创建素材状态事件/outbox 表和索引。

## GitHub-first 部署步骤

1. 确认本地基线 `app.py` SHA-256 与部署前线上文件完全一致。
2. 完成本地测试、提交并推送 GitHub。
3. 记录线上服务状态、源文件 SHA-256 和当前配置。
4. 查询 outbox，确认不存在携带旧八字段 payload 的
   `queued/retry/processing` 事件；若非 0，先完成旧事件投递，不直接切换。
5. 在 `/mnt/data-disk` 创建带校验清单的回滚备份，至少包含：
   - `app.py`
   - 新增 feature 目录的目标状态
   - `.env`
   - Nginx 配置
   - SQLite 在线备份
6. 从精确 GitHub commit 构建发布目录，只安装本需求涉及的代码和配置，不覆盖无关线上复合文件。
7. 首次发布时原子更新 `.env` 并生成高熵 Token；十字段增量不轮换 Token。
8. 首次发布时安装 Nginx exact-location 配置；十字段增量只复核 `nginx -t`。
9. 先执行生产代码编译和测试，再重启 `drama-material-api.service`。
10. 验证 `/api/auth/status`、新接口错误 Token、旧八字段 `422`、合法十字段入队、兜底群 canary 和日志。
11. 发布无 Token 的 HTML 接口文档。

## 验证步骤

```bash
python3 -m py_compile app.py features/material_status_broadcast/service.py
python3 scripts/test_material_status_broadcast.py
python3 scripts/test_material_status_webhook_app.py
nginx -t
systemctl status drama-material-api.service --no-pager
journalctl -u drama-material-api.service -n 200 --no-pager
curl -sS -i http://127.0.0.1:8787/api/auth/status
```

安全 canary：

- 错误 Token 必须返回 `401`，且事件表无新增。
- 使用 `TEST-` 资源、不存在的测试优化师和完整十字段提交一次合法事件。
- 接口返回 `202` 后，兜底群出现一条明确标注联调测试的消息。
- 飞书回读确认资源名、剧集配音类型、任务类型及其余字段完整展示。
- 相同幂等键重放不产生第二条正常播报。
- 日志和 SQLite 不包含 Token 或完整 open_id。

## 回滚方案

1. 停止新请求入口：移除本需求 Nginx exact-location 并 reload。
2. 恢复备份的 `app.py`、`.env` 和 Nginx 配置。
3. 保留新增 SQLite 表，不删除业务审计数据。
4. 执行 `nginx -t`，重启 `drama-material-api.service`。
5. 验证 `/api/auth/status` 和既有业务接口。
6. 如需重放未完成事件，在修复版本恢复后由 outbox 继续处理；不得直接删除或手工复制事件。

## 注意事项

- 不在命令行、Git 提交、PR、日志、接口文档或最终报告中显示真实 Token。
- 不向任意员工发送私聊 canary；需要用户指定安全测试账号。
- SQLite 备份必须使用在线 backup API 或停服务后的完整副本，不能复制正在写入的单一数据库文件后直接视为可恢复备份。

## 2026-07-28 十字段增量实例

- 精确提交：`8af21dbead5fd6fcf5f048319d76971573def77c`
- 发布目录：`/root/releases/drama-material-service-8af21dbe-material-status-10fields`
- 备份目录：`/mnt/data-disk/backups/drama-material-service/material-status-10fields-20260728T172922-pre-8af21dbe`
- 仅替换 `features/material_status_broadcast/service.py`、两份专项测试和公开 HTML；
  `app.py`、`.env`、Nginx 配置、MySQL、SQLite 结构均未修改。
- 服务只重启 `drama-material-api.service`；17:32:04 恢复 active。
- Token 数量与指纹前缀保持 `1 / ecd760a9d90b8399`，未轮换。
- 回滚时从上述备份恢复同名四个目标文件并重启 API；SQLite 业务事件保留。
