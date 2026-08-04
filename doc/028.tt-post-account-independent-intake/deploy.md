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

## 生产结果（2026-08-04）

- 代码 commit：`32f1f2875a569531f697bad93e84f61e5a2a91eb`；部署前已验证 GitHub 远端分支指向该 SHA。
- 新 release：`/opt/tt-post/releases/32f1f2875a569531f697bad93e84f61e5a2a91eb`；旧 release：`/opt/tt-post/releases/f91a3e1ae82c9843b37145d60c3fe5c188a8fea3`。
- 备份：`/mnt/data-disk/tt-post-publisher/backups/20260804T033644Z-f91a3e1-to-32f1f28-account-independent`；SQLite online backup `integrity_check=ok`。
- 服务器候选版本 9 个 Python 脚本 344/344、Node 53/53，编译通过。
- `tt-post-service.service`、主 API、runner/prepare timer 均 active；sidecar `/health` 正常，Nginx 配置通过。
- release、主应用和 Nginx 三份静态页 SHA-256 均为 `14ee10ddc4cfa0d6e2299228a2a0212c8faaba4ff2a0863e41a9a9d824227d00`；公网 200 且 hash 一致。
- 只读 GET：accounts=23、auto-config version=4/enabled=true、material-pool=5、direct-tests=1、tasks=5、queue=4。
- 部署前后 10 张 TT 表所有业务字段一致；只有既有 minutely runner 对已完成行的 `updated_at/consumed_at_utc/finished_at_utc` 自然刷新。11:39、11:40 tick 均为 0 个 publish/direct/reconcile 请求。
- 第一次切换因服务刚重启时立即探测端口而自动回滚；旧 release、旧静态页和服务核对恢复。第二次改为最多 30 秒健康轮询后成功。
- 验收未调用 POST、未保存配置、未加入素材、未创建或消费发布任务；GPU 未修改或重启。

## 精确回滚命令

```bash
ln -s /opt/tt-post/releases/f91a3e1ae82c9843b37145d60c3fe5c188a8fea3 /opt/tt-post/current.rollback-next
mv -Tf /opt/tt-post/current.rollback-next /opt/tt-post/current
cp -a /mnt/data-disk/tt-post-publisher/backups/20260804T033644Z-f91a3e1-to-32f1f28-account-independent/app-tt-post-pool.html /root/drama_material_service/static/tt-post-pool.html
cp -a /mnt/data-disk/tt-post-publisher/backups/20260804T033644Z-f91a3e1-to-32f1f28-account-independent/nginx-tt-post-pool.html /usr/share/nginx/html/tt-post-pool.html
systemctl restart tt-post-service.service
```

普通回滚不恢复 SQLite backup，因为本次无 schema 变更且生产业务数据不得倒退。
