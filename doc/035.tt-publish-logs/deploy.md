# 部署文档

## 变更内容

统一 TT 发布日志新增“4位码”列；自动模板从共享 code 路由补读。提供一次性、强门禁的历史已发布排期 code 回填。

## 配置项

无新增配置。复用 `TT_AUTO_POST_DB_PATH`、`TT_AUTO_POST_LEGACY_DB_PATH` 与现有 sidecar 内部令牌。

## 数据库变更

无表结构迁移。一次性回填只新增 `tt_post_code_route` 路由并写对应 `tt_post_queue.code`；不修改历史 caption、`long_url`、短链、状态或 `publish_id`，不处理 `tt_post_direct_test`。

## 部署步骤

1. 合并并拉取已验证 GitHub commit。
2. 记录并备份当前 TT auto sidecar release 指针，以及 `/usr/share/nginx/html/tt-publish-logs.{html,css,js}` 三个静态文件。
3. 从已推送 GitHub commit 建立新的 TT auto 不可变 release，运行 Python 编译与 TT 发布日志测试；原子切换 `/opt/tt-auto-post/current`，只重启 `tt-auto-post-service` 并确认 `/health`。
4. 从同一 commit 原子安装三个静态文件。现网 nginx 已有 `/tt-publish-logs.html` 精确 location，本变更不修改 nginx 配置，因此无需 reload；HTML 使用版本 query 强制刷新 JS/CSS。
5. `app.py` 和主 API 代理未变，不切换或重启 `drama-material-api.service`；`/opt/tt-post/current` 也不切 release。
6. 仅通过后台页面和只读 GET 验证，不触发真实发布。
7. 默认无参数 discovery 遇到 q2–q4 空 `long_url` 会按设计 fail-closed；候选集合先由只读 SQL 核对，再直接运行下面带明确 `--queue-id` 的 exact dry-run。空长链 q2–q4 必须逐条附带 ledger reconstruction 参数；q5–q7 复用各自冻结的 `AIpost` 长链。
8. apply 必须使用 exact dry-run 返回的 count/hash，并指定不存在的备份路径：

```bash
python scripts/backfill_tt_published_codes.py --db-path "$TT_POST_DB_PATH" \
  --queue-id 2 --queue-id 3 --queue-id 4 --queue-id 5 --queue-id 6 --queue-id 7 \
  --reconstruct-route-from-ledger-queue-id 2 \
  --reconstruct-route-from-ledger-queue-id 3 \
  --reconstruct-route-from-ledger-queue-id 4
python scripts/backfill_tt_published_codes.py --db-path "$TT_POST_DB_PATH" \
  --queue-id 2 --queue-id 3 --queue-id 4 --queue-id 5 --queue-id 6 --queue-id 7 \
  --reconstruct-route-from-ledger-queue-id 2 \
  --reconstruct-route-from-ledger-queue-id 3 \
  --reconstruct-route-from-ledger-queue-id 4 \
  --apply --expected-count COUNT --expected-hash SHA256 \
  --backup-path /mnt/data-disk/tt-post-publisher/backups/tt-post-before-code-backfill-TIMESTAMP.sqlite3
```

9. apply 后窄重启 `tt-post-service.service`，使该进程内的 code resolver cache 失效；不重启主 API、不全局清 Redis。确认 `tt-auto-post-runner.path` 为 `active (waiting)`，必要时显式 start。

## 验证步骤

- 检查新页面 200、登录/权限门禁和两类来源。
- 检查旧发布池不再显示/请求任务日志。
- 检查两类来源计数和最近任务与账本一致。
- 检查 `tt-auto-post-service`、`tt-post-service` 日志无敏感信息和异常，主 API 保持原进程未动。
- 核对回填输出的 `queue_id / publish_id / content_id / code`，逐个通过公共 resolver 验证 code 精确指向对应 Drama ID。
- 核对 q2–q4 的 `route_source=publish_recurring_v1`、唯一 recurring/event ID 和 fallback provenance；明确这些是 ledger-only 确定性替代路由，不是恢复原始长链。q5–q7 必须为 `route_source=frozen_long_url`。
- 核对候选已归零，队列总数、`publish_id`、caption、短链和自动模板高位路由基线未变化。

## 回滚方案

代码回滚：恢复部署前 TT auto release 指针和三个静态文件，重启 `tt-auto-post-service`；`tt-post-service` 仅需重启以旋转 resolver cache，主 API 无需处理。

数据回滚：优先按 apply 输出逐条删除本次新 route 并清空对应 `tt_post_queue.code`，事务内核对精确 code/queue 身份；只有在确认备份之后没有其他 TT 写入时，才可停服务并整体恢复 SQLite 备份，禁止在线盲目覆盖。

## 注意事项

回填不会自动修改 TikTok 帖子；运营人员使用页面映射手工替换帖子文案。
