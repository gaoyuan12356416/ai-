# 部署文档

## 变更内容

- 新增昨日 W2A 高花费剧只读离线刷新器。
- 新增数据盘 last-known-good JSON。
- 新增 `/api/public/tt-drama/featured` Nginx 静态路由。
- 将 `/tt` 的人工静态卡升级为动态可点击卡；请求失败时保留原静态卡。
- 新增每天上海时间 `15:30` 和 `18:00` 的 systemd timer。

## 配置项

默认值均记录在 `.env.example`。生产复用现有 `DRAMA_DB_*` 或
`ADMIN_MAPPING_MYSQL_*` 只读凭据；代码强制
`101.32.56.53:63350/kunlunads_dev` 并校验 `@@read_only=1`。

关键固定值：

- cache：`/mnt/data-disk/tt-drama-featured/public/current.json`
- product：`Dramawave`
- source app：`[w2a]drama-double`
- data source：`6`
- drama app：`1479`
- candidate/final：`20/5`
- data disk UUID：`3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`

## 数据库变更

无 DDL、DML 或状态修改。刷新任务只连接
`101.32.56.53:63350`，设置只读事务并检查 `@@read_only=1`。

## 部署步骤

1. 本地测试、代码评审通过后提交并推送 GitHub，记录精确 commit。
2. 只读记录生产 Nginx、静态文件、服务、timer、磁盘和当前 hash。
3. 在数据盘创建 release 与 backup；备份将变更的静态/Nginx 文件及
   systemd unit 的存在状态，生成 SHA-256 manifest。
4. 从 GitHub 检出精确 commit 到
   `/mnt/data-disk/tt-drama-featured/releases/<sha>`，将 `current` 原子切到
   该 release；不复制本地未提交文件。
5. 创建无登录权限的 `tt-drama-featured` 系统用户；从现有 `.env` 原样提取
   仅 `DRAMA_DB_*` / `ADMIN_MAPPING_MYSQL_*` 行到 root-owned `0600`
   `/etc/tt-drama-featured.env`，不得复制 COS、Feishu、OAuth 等其他秘密。
6. 只安装下列系统/公开文件，不同步整个多功能生产目录：
   - `deploy/tt-drama-featured.service`
   - `deploy/tt-drama-featured.timer`
   - `static/tt-drama-search.html`
   - `static/tt-drama-search.js`
   - `deploy/nginx/tt-drama-search.conf`
7. 预先创建 `/mnt/data-disk/tt-drama-featured/public`，owner 为
   `tt-drama-featured`、mode `0755`；确认挂载/UUID/空间和 Nginx 遍历权限。
   `ReadWritePaths` 的目标必须在启动 unit 前存在。
8. 先安装 service，`daemon-reload` 后手工 start；验证新 JSON 为目标昨日、
   5 条、无 spend、大小小于 32 KiB，并保存 hash。
9. 再发布两份 TT 静态文件和 Nginx 配置；`nginx -t` 成功后仅 reload Nginx。
10. 验证本地/公网 JSON、`/tt`、resolver 回归和真实浏览器卡片点击。
11. 最后 enable/start timer，确认 15:30/18:00 下一触发时间。

## 验证步骤

- `systemctl status tt-drama-featured.service --no-pager`
- `journalctl -u tt-drama-featured.service -n 100 --no-pager`
- `systemctl list-timers tt-drama-featured.timer --all --no-pager`
- `nginx -t`
- `curl -i http://127.0.0.1/api/public/tt-drama/featured`
- `curl -i https://ai.yingliangads.com/api/public/tt-drama/featured`
- JSON 断言：版本 1、目标昨日、5 个唯一合法 ID、无 `spend`。
- 发布闸门：对最终 Top20 SQL 做生产只读 EXPLAIN 和两次执行，确认无
  MySQL 1247/1055、使用 `as` 索引且结果签名稳定。
- `/tt?af_adset_id=XXX`：卡片 href 的三个核心键与透传参数正确。
- resolver 正确/不存在 ID 均保持原状态；`drama-material-api.service`
  `NRestarts` 不增加。
- 故障注入：使用不存在的 source_date 做 dry-run/受控失败，确认
  `current.json` SHA-256 不变。

## 回滚方案

1. `systemctl disable --now tt-drama-featured.timer`。
2. 从数据盘 backup 恢复两份 TT 静态文件和 Nginx 配置。
3. `nginx -t && systemctl reload nginx`。
4. 停止新 oneshot unit，恢复/删除本次新增 unit 后 `daemon-reload`。
5. 保留数据盘缓存供审计，不删除远端或本地业务数据。
6. 验证 `/tt` 回到旧静态卡、搜索 resolver 正常。

## 生产记录（2026-07-27）

- 源 commit：
  `bfe4bc499b95470dba55ff158015b7f5b5ea113c`
- release：
  `/mnt/data-disk/tt-drama-featured/releases/ai-tt-featured-bfe4bc499b95470d`
- backup：
  `/mnt/data-disk/tt-drama-featured/backups/20260727T151000+0800-bfe4bc4`
- pre/post manifest：
  `pre-deploy.sha256` / `post-deploy.sha256`
- 快照：
  `2026-07-26`、5 条、1,102 bytes、SHA-256
  `37e3a126a258e03b89ec743f08300e9d5582dc07f92916349b45c7dec2f5b2df`
- timer：enabled/active；首次计划任务在
  `2026-07-27 15:30:00 CST` 自动触发，8 秒成功且 `changed=false`；
  下一次为同日 `18:00` 对账。
- 主 API：active，`NRestarts=0`；仅 reload Nginx。
- 根盘仍约 92%；release、backup、cache 均在 6% 使用率的数据盘。

精确回滚：

1. `systemctl disable --now tt-drama-featured.timer`
2. 从 backup 将 `root-*`、`nginx-*` 两组静态文件及
   `tt-drama-search.conf` 恢复到原路径。
3. `nginx -t` 后 `systemctl reload nginx`。
4. `systemctl stop tt-drama-featured.service`；新 unit/env/current/cache
   可保留审计，不需要删除业务数据。
5. 验证 `/tt` 回到旧人工卡、resolver 正确/404 均正常、主 API
   `NRestarts` 不增加。

## 注意事项

- 当前根盘使用率 92%，release、backup、缓存不得写入根盘。
- 首次上线必须先生成快照，再发布前端和 Nginx，避免动态接口空窗。
- `Persistent=true` 首次启用前必须先手工验证，避免未审计补跑。
- 日表在次日中午后仍可能回填；15:30 是主快照，18:00 是同日对账。
