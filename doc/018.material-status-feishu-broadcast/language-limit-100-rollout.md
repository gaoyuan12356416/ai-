# language 字段长度上限调整为 100 的发布记录

## 发布结果

- 发布时间：2026-08-06 12:01:48（Asia/Shanghai）
- GitHub 分支：`codex/material-status-language-100-20260806`
- 精确提交：`cf96751d4748f4a974cd834665d26e9e84081a4f`
- 生产发布目录：`/root/releases/drama-material-service-cf96751d-material-status-language-100`
- 生产备份目录：`/mnt/data-disk/backups/drama-material-service/material-status-language-100-20260806T120143-pre-cf96751d`

`POST /api/integrations/v1/material-task-status-events` 的 `language` 字段在 NFC
规范化并去除首尾空白后，长度上限由 32 个字符调整为 100 个字符。其余九个字段、
32 KiB 请求体上限、鉴权、幂等、outbox 和飞书投递规则均未改变。

## 发布范围

- `features/material_status_broadcast/service.py`
- `scripts/test_material_status_broadcast.py`
- `scripts/test_material_status_webhook_app.py`
- `/usr/share/nginx/html/docs/material-status-api/index.html`

未修改 `app.py`、`.env`、Nginx 配置、MySQL 或 SQLite 表结构；仅重启
`drama-material-api.service`，未发送真实播报 canary。

## 验证证据

- 本地相关回归：139/139 通过。
- GitHub 精确提交在发布目录内的专项测试：28/28 通过。
- 安装后的生产目录专项测试：28/28 通过。
- `language` 为 100 个 Unicode 字符：接收。
- `language` 为 101 个 Unicode 字符：`422 invalid_payload`。
- 错误 Bearer Token：`401 invalid_token`，没有写入 outbox。
- 发布前后 outbox 均为 `delivered=814`、`queued/retry/processing=0`。
- SQLite 在线备份 `PRAGMA quick_check=ok`，备份 `SHA256SUMS` 校验通过。
- 公开文档 HTTP 200，公网响应与服务器文件逐字节一致。
- 服务自 2026-08-06 12:01:48 起为 `active/running`，端口 8787 返回 200。
- 发布后 `warning` 及以上 journal 记录为 0。

首次在服务启动后 22ms 立即探测 8787 时端口尚未监听；服务随后完成启动，重试
返回 200。该启动探测竞争未触发重复安装或回滚。

## 生产文件 SHA-256

- `service.py`：`836138bb2572c9d0607525aa24412d27c35c1de4f6d7fbbe18199448b78319f4`
- `test_material_status_broadcast.py`：`697ea40b629e1670ae64a32f4813686d57fb5027691eb9da2a5efb13087147b4`
- `test_material_status_webhook_app.py`：`009db8c46fb30dda2ed99460d71c927db27510a9a406dcaf596fc36fc2daa0fb`
- 公开 HTML：`7a6a602d24c00f28936e74e1b615fc6d61da69b6226b71304b775bb26bcabc4a`

## 回滚

从上述备份目录恢复三个代码/测试文件和公开 HTML，然后仅重启 API：

```bash
backup=/mnt/data-disk/backups/drama-material-service/material-status-language-100-20260806T120143-pre-cf96751d
install -m 0644 "$backup/live/features/material_status_broadcast/service.py" /root/drama_material_service/features/material_status_broadcast/service.py
install -m 0644 "$backup/live/scripts/test_material_status_broadcast.py" /root/drama_material_service/scripts/test_material_status_broadcast.py
install -m 0644 "$backup/live/scripts/test_material_status_webhook_app.py" /root/drama_material_service/scripts/test_material_status_webhook_app.py
install -m 0644 "$backup/public-doc/index.html" /usr/share/nginx/html/docs/material-status-api/index.html
python3 -m py_compile /root/drama_material_service/app.py /root/drama_material_service/features/material_status_broadcast/service.py
systemctl restart drama-material-api.service
systemctl is-active drama-material-api.service
```

回滚不删除或修改 SQLite outbox 历史记录。
