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

本开发任务默认只提交和推送分支，未经用户再次授权不部署生产。
