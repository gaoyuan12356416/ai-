# 部署记录

## 变更内容

- CPU TT Post sidecar：配置保存、素材入池和 recurring preparation 去除 Creator Info 依赖。
- 管理页：素材入池不再要求选择“本次测试账号”。
- 无数据库 schema、环境变量、GPU release、Token 或调度器变更。

## GitHub-first 门禁

- 分支：`codex/tt-post-account-independent-intake-20260804`。
- 候选 commit：部署时记录最终 SHA，并验证远端分支指向一致 SHA。
- 本地 9 个 Python 测试脚本 344/344、Node 53/53、编译与差异检查通过。

## 生产目标

- CPU：`43.166.187.96`。
- 当前 release：部署前只读记录 `/opt/tt-post/current`。
- SQLite：`/mnt/data-disk/tt-post-publisher/tt-post.sqlite3`，部署前 online backup。
- 静态页：release 副本、`/root/drama_material_service/static/tt-post-pool.html`、`/usr/share/nginx/html/tt-post-pool.html`。
- 服务：只重启 `tt-post-service.service`；主 API、runner/timer、GPU 不因本变更重启。

## 部署步骤

1. 只读记录 release、服务/timer、静态页 hash、数据库完整性和业务表逻辑指纹。
2. 创建 SQLite online backup 和三份静态页/当前 symlink 回滚点。
3. 从 GitHub 拉取已验证 commit，创建 `/opt/tt-post/releases/<commit>`。
4. 在候选 release 运行编译和无网络合同测试。
5. 原子切换 `/opt/tt-post/current`，同步两份外部静态页，只重启 TT Post sidecar。
6. 验证服务 health、页面 200/hash、日志无 route/schema 错误。
7. 仅执行 GET 的生产验收，并比较部署前后数据库逻辑指纹。

## 回滚

- 将 `/opt/tt-post/current` 原子切回部署前 release。
- 恢复部署前备份的三份静态页。
- 重启 `tt-post-service.service` 并复核 health/hash。
- 本次无 schema 变更，普通回滚不覆盖 SQLite backup、不修改 GPU ledger。

## 生产结果

待部署后补充：最终 commit、release、backup、hash、服务状态、只读 0 副作用证据和精确回滚命令。
