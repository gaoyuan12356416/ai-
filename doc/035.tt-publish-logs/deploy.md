# 部署文档

## 变更内容

新增统一 TT 发布日志只读接口和静态页面；旧发布池移除日志展示。

## 配置项

无新增配置。复用 `TT_AUTO_POST_DB_PATH`、`TT_AUTO_POST_LEGACY_DB_PATH` 与现有 sidecar 内部令牌。

## 数据库变更

无迁移、无写入、无回填。

## 部署步骤

1. 合并并拉取已验证 GitHub commit。
2. 备份当前主 API 和 TT auto sidecar release 指针及相关静态文件。
3. 建立新不可变 release，运行 Python 编译与 TT 发布日志测试。
4. 安装 `deploy/nginx-tt-auto-publish.conf` 中新增的 `/tt-publish-logs.html` 精确 location，执行 `nginx -t` 后 reload。
5. 先切换 `tt-auto-post-service`，确认 `/health`；再切换并重启 `drama-material-api.service`。
6. 仅通过后台页面和只读 GET 验证，不触发真实发布。

## 验证步骤

- 检查新页面 200、登录/权限门禁和两类来源。
- 检查旧发布池不再显示/请求任务日志。
- 检查两类来源计数和最近任务与账本一致。
- 检查 sidecar、主 API 日志无敏感信息和异常。

## 回滚方案

恢复部署前 release 指针，依次重启 `tt-auto-post-service` 与 `drama-material-api.service`。数据库没有变化，无数据回滚。

## 注意事项

生产验收只允许读取账本和页面；不得为了验证本功能创建发布请求或触发真实 TikTok 发布。

## 2026-08-06 生产部署记录

- 代码 release：`74ad5639bb90cd0c1d6777a1fc3862e0f063ecec`；路径：`/opt/tt-auto-post/releases/74ad5639bb90cd0c1d6777a1fc3862e0f063ecec`。
- 上一 release：`5b18d1ef68614ae01bf97a7e092bcd0d9c345d3f`。
- 回滚备份：`/mnt/data-disk/tt-auto-post-deploy/backups/20260806-170753-unified-publish-logs-pre`；包含两个 SQLite 在线副本、切换前自动任务快照、主 API 受影响文件、两套静态文件、Nginx 配置、release/PID/门禁状态和已通过的 `SHA256SUMS`。数据库副本 `quick_check=ok`。
- 最终切换前临时暂停 runner timer/path，确认没有已 claim 或处于准备/发布关键阶段的自动任务后切换；完成后恢复为 `active` / `active (waiting)`。
- 新页面 `https://ai.yingliangads.com/tt-publish-logs.html` 返回 200，并带 `no-cache, no-store`；内部只读接口验收为总计 62、素材池 61、自动发布 1。
- 登录态浏览器验收显示 20 行首屏数据及相同来源统计；旧发布池中 `queueFilters`、`queueRows` 和“发布任务”区域已移除，保留指向新页面的“发布日志”入口。
- `tt-post-service.service` 未重启，PID 保持 `3055551`；自动发布三重门禁保持 0。未执行真实发布，也未迁移或改写任一账本。
- 两次正式切换前的验收误判均按预案自动回滚：一次是 `curl | grep` 在 `pipefail` 下的提前关闭，另一次是 PowerShell 传递中文 grep 模式发生编码失真。最终改为落盘后读取和 Unicode 转义校验，业务文件无需因此修改。

精确回滚时，先暂停 runner timer/path并确认无 claim/准备/发布中的自动任务，再恢复备份中的主 API、静态文件及 Nginx 配置，把 `/opt/tt-auto-post/current` 原子指回上一 release，依次重启 `tt-auto-post-service.service` 与 `drama-material-api.service`，执行 `nginx -t` 后 reload，最后恢复 runner timer/path。数据库没有变更，不得用备份覆盖线上账本。
