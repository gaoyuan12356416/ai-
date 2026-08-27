# A组自动复刻播报 V1：部署与回滚

状态：已上线（2026-08-27 18:00:03，Asia/Shanghai），无真实测试播报。本文操作须由维护方执行；真实凭据不进入 Git、日志、截图或接口文档。

## 变更与边界

- 仅新增 `POST /api/integrations/v1/material-replication-events`、专属 Token、批次 outbox 和异步投递 worker。
- 复用既有账号映射缓存、邮箱查询飞书用户及文本发送能力。旧接口字段、模板、队列和重试决策不变；共享异常对象只增加供新 worker 使用的发送确定性元数据。
- 不改前端，不改 GPU worker、素材生产、投放定时器或上游表格。
- 不发送真实测试播报；生产只做必定拒绝的 401/422/413 请求校验。

## 经核对的发布基线

| 项目 | 值 |
|---|---|
| 主机 | CPU `43.166.187.96` |
| 运行目录 | `/root/drama_material_service`（非 Git 工作区） |
| 唯一需重启的业务服务 | `drama-material-api.service` |
| Python | `3.9.6` |
| 基线 Git 提交 | `ee6e00c000c31a538b9294a9da7f084dd9e5f9ac` |
| 原 app.py SHA256 | `7dee7b9542a52b97842608207076b283d33e3353b46d3d2169a6d09ed11e5486` |
| 旧播报 service.py SHA256 | `6500dcb230a8f7c8e0f4d17a055c359b085683cf2f6f2642aff32a2f1b8232b0` |
| 现有队列 DB | `/root/drama_material_service/data/drama_material_jobs.sqlite3` |
| 发布及备份目录 | `/mnt/data-disk/material-replication-broadcast` |

发布脚本会再次检查真实文件哈希和业务队列；任何基线漂移、进行中的生产任务、待处理的旧/新播报都拒绝重启，需重新核对后安排窗口，不强制清队列。

## 新增配置

| 文件 | 用途 |
|---|---|
| `/etc/material-replication-webhook.env` | `MATERIAL_REPLICATION_WEBHOOK_TOKENS`，新生成独立随机 Token，权限 `0600` |
| `/etc/systemd/system/drama-material-api.service.d/60-material-replication-webhook.conf` | 仅给主 API 加载上面的环境文件 |
| `/etc/nginx/default.d/material-replication-webhook.conf` | 精确新路由、32 KiB 请求上限、JSON 413 错误响应 |

不覆盖原 `.env` 或其他服务 drop-in；继续使用原有飞书应用、账号映射数据库和 `MATERIAL_STATUS_WEBHOOK_FALLBACK_CHAT_ID`。不把旧 Token 复制给新接口。

## 数据库变化与备份

新增 `material_replication_broadcast_outbox` 及其索引，所有操作与旧十字段 outbox 隔离。只在新功能配置完整时初始化；不修改旧表结构或行。

变更前由 `scripts/deploy_material_replication.py`：

1. 校验挂载盘 UUID、空间、GitHub release 提交与干净状态。
2. 将原 `app.py`、旧播报模块、现有主服务/相关 Nginx/环境配置复制至权限 `0700` 的备份目录；配置副本权限 `0600`。
3. 使用 SQLite 在线 backup API 备份现有 DB，不直接复制可能存在 WAL 的活动数据库。
4. 对备份执行 `PRAGMA quick_check`；另取副本演练建表并核对旧队列状态计数未变。
5. 保存 `manifest.json`：提交、部署文件校验和、配置校验和、备份校验和、原服务 PID、原队列计数。不记录 Token 原文。
6. 再次检查线上基线与空闲条件，然后原子安装模块/配置/主程序。

## 发布流程

先完成需求、SA、QA、代码评审与离线回归，提交并推送 GitHub。服务器从该分支拉取独立、干净的 release，校验完整提交 SHA；禁止把本地未提交脚本直接同步进运行目录。

在精确 release 根目录执行（`<完整提交SHA>` 必须替换为真实已推送提交）：

```bash
python3 scripts/deploy_material_replication.py apply \
  --commit <完整提交SHA> \
  --expected-app-sha256 7dee7b9542a52b97842608207076b283d33e3353b46d3d2169a6d09ed11e5486 \
  --expected-legacy-sha256 6500dcb230a8f7c8e0f4d17a055c359b085683cf2f6f2642aff32a2f1b8232b0
```

脚本在生产写入前打印 `prepared_backup` 路径。只重启主 API，并在 `nginx -t` 通过后 reload Nginx。若中途失败，按该路径检查 manifest 和实际阶段，再使用下面的受控回滚；不再次覆盖运行 apply。

## 无真实消息的上线验证

脚本分别从 `127.0.0.1:8787` 与生产 HTTPS 地址验证：

- 新接口不带 Token：`401 invalid_token`。
- 新接口带正确专属 Token，但 `items=[]`：`422 invalid_payload`。
- 新接口带正确 Token，但正文 32,769 字节：`413 payload_too_large`。
- 旧接口不带 Token：`401 invalid_token`。

校验请求不跟随重定向，不把 Token 转发至其他 URL。共 8 个拒绝请求，任何响应都不得产生批次。验证同时检查主服务 active、已安装文件哈希、旧播报模块哈希、新 outbox 已完成初始化且零记录，保存 `verification.json`。缺表不能等同于空队列，初始化失败必须使上线验收失败。

```bash
python3 scripts/deploy_material_replication.py verify --backup <prepared_backup绝对路径>
```

此项仅证明部署、路由、鉴权、校验和队列隔离正常。私聊/兜底实际送达不做擅自试发，使用首个获准的真实业务批次验收；`202` 不等于送达。

## 回滚

```bash
python3 scripts/deploy_material_replication.py rollback --backup <prepared_backup绝对路径>
```

- 回滚路径必须位于本功能精确备份目录下；核对所有备份哈希。
- 主程序必须仍为本次部署或原基线哈希；新增配置必须仍与 manifest 一致，防止撤销其他任务的后续变更。
- 和发布一样，常规回滚先检查无在途正式任务/待处理播报，条件不满足则停止，不强杀业务任务。
- 恢复本次备份的原 `app.py`，将本功能新增 systemd/Nginx 配置移动到备份目录的 `withdrawn-*` 文件中。该撤回可恢复，不删除配置。
- **绝不使用部署前 DB 覆盖线上 DB**。保留所有已接收批次、幂等键、冻结目标、正文、UUID、发送确定性和审计；保留新模块与凭据文件，停用能力而不清数据。
- 新模块已复制但主程序未切换的部分部署，也允许按上述已知哈希条件撤回。
- 重新上线时先对账 `delivery_unknown`、`dead_letter` 及待处理批次；不能重置状态、换键或跨安全窗口自动重发。

## 实际发布记录

| 项目 | 实际结果 |
|---|---|
| GitHub 发布提交 | `0a391260f6de1d2e99b351b21d41a613866a5cfb` |
| GitHub 分支 | `codex/material-replication-broadcast-20260827`，已推送 |
| 精确 release | `/mnt/data-disk/material-replication-broadcast/releases/0a391260f6de1d2e99b351b21d41a613866a5cfb` |
| 备份目录 | `/mnt/data-disk/material-replication-broadcast/backups/20260827-175953-pre-0a391260`，权限 `0700` |
| 备份与迁移演练 | 在线 SQLite backup、quick_check=ok、在副本上建表且旧队列计数不变 |
| 文件核对 | 全部已部署文件、备份文件和新增配置 SHA256 匹配 manifest；旧播报 service.py 未变化 |
| 主服务 PID | 变更前 `1116402` → 变更后 `1123265`，active |
| 新 worker | 日志 `2026-08-27 18:00:01,059 ... material replication batch worker configured=True`，启动失败记录为 0 |
| Token | 仅服务器 `/etc/material-replication-webhook.env`，权限 `0600`；真实值不进入任何交付文档 |
| Nginx | `nginx -t` 通过、reload 完成、active |
| 其他生产 worker | `drama-material-job-worker.service` 仍 active；本次未重启该服务 |
| 上线拒绝验证 | 8/8 通过；详见下表与 [机器可读记录](evidence/deployment-verification.json) |
| 新队列 | 表已存在、0 条批次，无测试入队 |
| 旧队列 | 变更前后均 `delivered=6410`，无待处理记录；生产/截图/广告素材任务状态计数不变 |
| CPU 同范围回归 | Python 3.9.6，244/244 PASS，16.792 秒，失败/错误/跳过/外网调用均 0 |
| CPU 回归日志 | `/mnt/data-disk/material-replication-broadcast/qa/final.qOYRqR/regression.log` |

| 请求 | 本机 8787 | 生产 HTTPS |
|---|---|---|
| 新接口，无 Token | `401 invalid_token` | `401 invalid_token` |
| 新接口，专属 Token + 空 items | `422 invalid_payload` | `422 invalid_payload` |
| 新接口，专属 Token + 32,769 字节 | `413 payload_too_large` | `413 payload_too_large` |
| 旧接口，无 Token | `401 invalid_token` | `401 invalid_token` |

生产服务器保留完整 `manifest.json` 和 `verification.json`。本地独立评审及完整统计见 [测试报告](test-report.md)。回滚脚本已通过隔离 Mock 对完整/部分发布、配置漂移和活动任务门禁的测试；**没有在生产执行回滚演练**。

本版本可供上游接入，但首次真实业务批次的飞书私聊/兜底送达仍需按授权进行业务验收。部署过程没有提交任何合法生产批次，不能将这些拒绝校验冒充生产 `202` 或实际送达验证。

本次发布的精确回滚命令（先检查活动任务与源文件/配置漂移）：

```bash
cd /mnt/data-disk/material-replication-broadcast/releases/0a391260f6de1d2e99b351b21d41a613866a5cfb
python3 scripts/deploy_material_replication.py rollback \
  --backup /mnt/data-disk/material-replication-broadcast/backups/20260827-175953-pre-0a391260
```
