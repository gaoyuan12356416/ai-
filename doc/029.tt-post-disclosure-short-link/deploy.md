# 部署文档

## 变更内容

TT sidecar/core/link helper、Nginx TT snippet、TT 发布池页面。

## 数据库变更

无 schema 或手工数据变更。

## 部署前备份

- 当前 `/opt/tt-post/current` release。
- SQLite online backup 与 integrity check。
- `/mnt/data-disk/tt-post-publisher/s2l`、`/mnt/data-disk/x-post-automation/s2l` 文件清单/hash。
- Nginx TT snippet、三份 TT 静态页。

## 部署步骤

1. 推送并核对 GitHub commit。
2. 创建不可变候选 release，运行编译和全量 TT 测试。
3. 更新精确 Nginx snippet，`nginx -t`。
4. 切换 release，同步静态页，重启 `tt-post-service.service`，reload Nginx。
5. health、公网页面、新旧路由合同和数据库业务字段只读验收。

## 回滚

切回上一 release，恢复 Nginx snippet/静态页并重启 sidecar、reload Nginx。无 schema 变化，普通回滚不恢复 SQLite。

## 生产结果

待部署后补充精确 commit、release、backup、hash 和只读验收结果。
