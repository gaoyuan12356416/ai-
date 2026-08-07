# 部署文档

## 变更内容

统一 TT 发布日志新增“4位码”列；自动模板从共享 code 路由补读。提供一次性、强门禁的历史已发布排期 code 回填。

## 配置项

无新增配置。复用 `TT_AUTO_POST_DB_PATH`、`TT_AUTO_POST_LEGACY_DB_PATH` 与现有 sidecar 内部令牌。

## 数据库变更

无表结构迁移。一次性回填只新增 `tt_post_code_route` 路由并写对应 `tt_post_queue.code`；不修改历史 caption、`long_url`、短链、状态或 `publish_id`，不处理 `tt_post_direct_test`。

## 部署步骤

1. 合并并拉取已验证 GitHub commit。
2. 备份当前主 API 和 TT auto sidecar release 指针及相关静态文件。
3. 建立新不可变 release，运行 Python 编译与 TT 发布日志测试。
4. 安装 `deploy/nginx-tt-auto-publish.conf` 中新增的 `/tt-publish-logs.html` 精确 location，执行 `nginx -t` 后 reload。
5. 先切换 `tt-auto-post-service`，确认 `/health`；再切换并重启 `drama-material-api.service`。
6. 仅通过后台页面和只读 GET 验证，不触发真实发布。
7. 对生产 SQLite 先运行不带 `--apply` 的 discovery；确认候选后用明确 `--queue-id` 再跑 exact dry-run。
8. apply 必须使用 exact dry-run 返回的 count/hash，并指定不存在的备份路径：

```bash
python scripts/backfill_tt_published_codes.py --db-path "$TT_POST_DB_PATH"
python scripts/backfill_tt_published_codes.py --db-path "$TT_POST_DB_PATH" \
  --queue-id ID1 --queue-id ID2
python scripts/backfill_tt_published_codes.py --db-path "$TT_POST_DB_PATH" \
  --queue-id ID1 --queue-id ID2 \
  --apply --expected-count COUNT --expected-hash SHA256 \
  --backup-path /mnt/data-disk/tt-post-publisher/backups/tt-post-before-code-backfill-TIMESTAMP.sqlite3
```

9. 重启 TT Post resolver 所在主 API，使旧 Redis negative/latest cache namespace 立即失效；不全局清 Redis。

## 验证步骤

- 检查新页面 200、登录/权限门禁和两类来源。
- 检查旧发布池不再显示/请求任务日志。
- 检查两类来源计数和最近任务与账本一致。
- 检查 sidecar、主 API 日志无敏感信息和异常。
- 核对回填输出的 `queue_id / publish_id / content_id / code`，逐个通过公共 resolver 验证 code 精确指向对应 Drama ID。
- 核对候选已归零，队列总数、`publish_id`、caption、短链和自动模板高位路由基线未变化。

## 回滚方案

代码回滚：恢复部署前 release 指针，依次重启 `tt-auto-post-service` 与 `drama-material-api.service`。

数据回滚：优先按 apply 输出逐条删除本次新 route 并清空对应 `tt_post_queue.code`，事务内核对精确 code/queue 身份；只有在确认备份之后没有其他 TT 写入时，才可停服务并整体恢复 SQLite 备份，禁止在线盲目覆盖。

## 注意事项

回填不会自动修改 TikTok 帖子；运营人员使用页面映射手工替换帖子文案。
