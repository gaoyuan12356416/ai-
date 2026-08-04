# 部署与回滚文档

## 状态

部署计划已编写，尚未执行。实际 commit、release、备份目录、服务 PID、hash 和命令结果必须在部署后补录；本文不得被解读为已上线。

## 变更范围

- TT sidecar：code schema、分配、发布快照、Redis 读缓存。
- 主 API：`/api/public/tt-code/resolve`。
- 静态：新 `tt-drama-code-search.html/js`，原 `/tt` 两文件不改。
- Nginx：新 `/tt-code`、新脚本和新 API exact location。
- 配置：`TT_POST_CODE_REDIS_*` 与独立本机 Redis 6381。
- SQLite：在现有数据盘 DB 加法创建 `tt_post_code_route` 和索引。

## GitHub-first 门禁

1. 在干净本地工作树完成实现和全量验证。
2. `git diff --check`、编译、自动化、浏览器回归全部通过后提交并 push GitHub。
3. 记录 exact commit SHA；服务器只能从该 SHA 创建不可变 release，不得把未提交本地文件直接复制成“已同步”。
4. 服务器部署前记录当前 `/opt/tt-post/current`、`/root/drama_material_service` 实际源 hash/commit、服务和定时器状态。
5. 生产真实 `.env`、Redis ACL/密码和内部 token 不进入 GitHub、日志、截图或文档。

## 部署前只读检查

### 主机与数据盘

```bash
hostname
findmnt -n -o TARGET,SOURCE,FSTYPE,UUID,OPTIONS /mnt/data-disk
df -h / /mnt/data-disk
test "$(findmnt -n -o UUID /mnt/data-disk)" = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
```

UUID、挂载、可写性或空间任一不满足时停止；不得回落到未挂载的 `/mnt/data-disk` 目录或根盘。

### 版本与服务

```bash
readlink -f /opt/tt-post/current
systemctl status tt-post-service.service --no-pager
systemctl status drama-material-api.service --no-pager
systemctl status tt-post-runner.timer tt-post-prepare.timer --no-pager
systemctl status nginx --no-pager
```

记录当前 release、MainPID、health、timer/path、Nginx config hash。不得手动触发 runner。

### 数据基线

使用 `sqlite3 -readonly` 或 SQLite URI `mode=ro`，记录但不打印 caption/完整 URL 中不必要的业务文本：

- `PRAGMA integrity_check`
- queue 总数、max queue ID、非空 TikTok `publish_id` 数
- `tt_post_code_route` 是否存在、行数、published 数、最大时间
- 运行/事件计数
- 原 `/tt` HTML/JS 的本地、Nginx 和公网无缓存 SHA-256

## 备份

备份根建议：

```text
/mnt/data-disk/tt-post-publisher/backups/<timestamp>-tt-code-search-pre-<old_sha>/
```

### 1. SQLite

- 使用 SQLite online backup 创建 `tt-post.sqlite3.pre`，不要在运行中普通 `cp` 活跃 DB。
- 对 backup 执行 `PRAGMA integrity_check`。
- 保存 `sqlite_master` schema、表/索引清单和非敏感计数。
- 生成相对路径 SHA-256 manifest 并在备份目录内校验。

### 2. 代码与静态

- 记录 `/opt/tt-post/current` 旧 release；无需复制不可变 release 本体，但必须确认仍存在且可读。
- 备份 `/root/drama_material_service/app.py` 及将变更的模块。
- 备份应用 static 与 `/usr/share/nginx/html` 下的 TT 页面文件。
- 对原 `tt-drama-search.html/js` 只做备份/hash，不覆盖。

### 3. Nginx、env、systemd、Redis

- 备份 `/etc/nginx/default.d/tt-drama-search.conf` 或实际 exact snippet。
- 备份 `/etc/tt-post.env`、主 API env、Redis 配置和 ACL；保持原 owner/mode，禁止输出内容。
- 备份 `tt-post-service.service`、`drama-material-api.service` 及新增/修改的 Redis unit。
- 保存 `systemctl cat` 和 `systemctl show` 的非敏感信息。

## Redis 6381 部署合同

- 独立实例只监听 `127.0.0.1:6381`（需要 IPv6 时只允许 `::1`）。
- `protected-mode yes`，不开放公网/安全组端口。
- 作为可丢弃读缓存，默认关闭 RDB/AOF 或将任何运行文件放到数据盘；不得把它当恢复源。
- 设置合理 `maxmemory` 和仅缓存淘汰策略；实际值按容量评估后补录。
- 若使用 ACL/密码，秘密只放 root-only 生产配置；`.env.example` 只留空占位。
- env 前缀固定：

```text
TT_POST_CODE_REDIS_HOST=127.0.0.1
TT_POST_CODE_REDIS_PORT=6381
TT_POST_CODE_REDIS_DB=<dedicated db>
TT_POST_CODE_REDIS_CONNECT_TIMEOUT_SECONDS=<bounded>
TT_POST_CODE_REDIS_READ_TIMEOUT_SECONDS=<bounded>
TT_POST_CODE_REDIS_POSITIVE_TTL_SECONDS=<bounded>
TT_POST_CODE_REDIS_NEGATIVE_TTL_SECONDS=<shorter bounded>
TT_POST_CODE_REDIS_NAMESPACE=<versioned namespace>
```

实际变量以最终代码为准，但必须保持 `TT_POST_CODE_REDIS_` 前缀和 6381 生产端口。

验证：

```bash
ss -lnt '( sport = :6381 )'
```

输出不得出现 `0.0.0.0:6381` 或公网地址。

## 候选 release 和迁移演练

1. 服务器用 GitHub deploy key `git fetch --all --prune`，校验 exact commit 存在。
2. 从 exact commit 创建 `/opt/tt-post/releases/<commit>`，目录/文件设为不可变可读模型；顶层至少 `0555`，并以 `tt-post` 用户验证可遍历和 import。
3. 在 SQLite online backup 的第二份副本上运行新 `ensure_storage()`。
4. 断言只新增 `tt_post_code_route`/索引，既有 queue/pool/event schema 与行计数不变，`integrity_check=ok`。
5. 使用隔离 Redis 端口/namespace 和 fake resolver 运行 code 分配、clone/fallback、陈旧缓存回归。
6. 候选目录执行与本地相同的编译和 TT 全量测试。

## 生产部署步骤

1. 再次确认 GitHub SHA、backup manifest、数据盘和零真实发布边界。
2. 安装/启用独立 Redis 6381 配置，但先保持应用仍走旧代码；验证仅 loopback。
3. 以 root-only 原子方式更新 `/etc/tt-post.env` / 相关 secret/env；不在 shell 输出值。
4. 安装新 Nginx snippet 到临时位置，执行 `nginx -t`；失败则不切换。
5. 原子切换 `/opt/tt-post/current` 到 exact release。
6. 只安装新 `tt-drama-code-search.html/js` 到应用 static 和 Nginx static；对原 `/tt` 文件执行 hash 断言，不写入。
7. 主 API 若有变更，安装 exact commit 对应文件。
8. `systemctl daemon-reload` 仅在 unit 变化时执行。
9. 重启 `tt-post-service.service`；如 `app.py` 变化，再重启 `drama-material-api.service`。不重启 GPU、不人工触发 runner/prepare。
10. `nginx -t` 再通过后 reload Nginx，不做宽泛重启。

## 验证步骤

### 服务和日志

```bash
systemctl status tt-post-service.service drama-material-api.service --no-pager
journalctl -u tt-post-service.service -n 200 --no-pager
journalctl -u drama-material-api.service -n 200 --no-pager
curl -fsS http://127.0.0.1:18829/health
nginx -t
```

日志检查需确认无 secret、Redis 密码、内部 token、SQL 或完整异常堆栈泄漏。

### API

- code exact：小写输入命中大写 code，`route_mode=code_exact`、channel TT。
- 直接 ID：分别验证 published clone 和 generic fallback，channel Search。
- Featured：分别验证 clone 和 fallback，channel Featured。
- 非法 query/source、404、Redis 停止/恢复、SQLite 故障模拟语义。
- 所有 target host/path/`af_dp` 与参数编码。

生产不得为验收临时创建 published code route；使用迁移前已有安全记录或隔离副本。若生产还没有记录，只验证 fallback 和 404，不伪造 published 状态。

### 页面

- 公网无缓存打开 `/tt-code`，390x844 与桌面视口检查。
- 动态/fallback 恰好五条。
- 触摸、鼠标拖动、左右按钮和键盘；拖动期间 resolver 调用为 0。
- code/ID 搜索成功、404、503 文案与 CTA fail-closed。
- `/tt` 原页面 hash、200/no Location 和真实搜索主流程保持不变。

### 数据与零发布

- SQLite `integrity_check=ok`，schema/索引正确。
- 迁移不会新增 queue、run、event 或 publish ID。
- 对比部署前后测试相关 queue/publish ledger；不得调用 publish/canary/run-now/schedule-save。
- 自然 scheduler 产生的无关业务变化需单独标识，不能冒充本需求验收结果。

## 回滚方案

### 普通代码回滚

1. 记录当前失败 release、服务状态和日志。
2. 原子把 `/opt/tt-post/current` 切回旧 release。
3. 恢复备份的主 API 文件、Nginx snippet、静态文件、env 和 unit。
4. 删除/下线新 `/tt-code` 静态入口；原 `/tt` 恢复/保持备份 hash。
5. 重启受影响 sidecar/API，`nginx -t` 后 reload。
6. 新加法表默认保留，不 DROP、不删除 code 路由；旧代码应忽略它。
7. 停止/禁用独立 Redis 6381（若旧代码不使用），清理缓存不影响 SQLite 事实数据。

### SQLite 恢复边界

- 普通代码回滚不恢复数据库备份，因为部署后 queue/publish ledger 可能已前进。
- 只有确认数据库损坏且获得明确批准时，才停止所有 DB writer，保存损坏副本，核对备份 manifest 后恢复 `tt-post.sqlite3.pre`。
- 若新代码已产生真实发布或 caption code，恢复旧 DB 可能丢失路由并使历史 code 失效，必须先逐条对账；本需求验收阶段禁止真实发布正是为了避免该状态。

### 精确回滚记录（部署后补录）

```text
GitHub commit:
old release:
new release:
backup directory:
old /tt hashes:
new /tt-code hashes:
Nginx backup:
env/systemd/Redis backup:
rollback performed/rehearsed:
```

## 发布后观察

- code 分配碰撞数、allocator fallback、空间占用率和 recycle 事件。
- Redis HIT/MISS/BYPASS、超时、陈旧失效失败和内存。
- API 2xx/4xx/5xx、p95、限流和 resolver 上游错误。
- `/tt` 与 `/tt-code` 可用性，Featured 五条完整率。
- 不以监控为由自动触发真实 TikTok 发布。
