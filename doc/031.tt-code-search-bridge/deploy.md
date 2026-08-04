# 部署与回滚文档

## 状态

部署方案已按当前实现更新，但尚未执行。本文件中的 commit、release、backup、PID、hash 和运行结果字段必须在实际部署后填写；任何计划步骤都不得描述成线上已通过。

## 变更范围

- `/opt/tt-post` sidecar：route/audit schema、queue.code、正式 queue 冻结、私有 resolver、Redis cache。
- `/root/drama_material_service/app.py`：公共组合 resolver、既有限流/并发门、sidecar bearer 调用、剧目和 target 校验。
- 静态：新增 `tt-drama-code-search.html/js`；更新管理页对 `{code}` 的说明/preview；原 `tt-drama-search.html/js` 不改。
- Nginx：新增 `/tt-code`、新 JS 和公共 API exact locations；公共 API 代理 `127.0.0.1:8787`。
- Redis：新增独立 `tt-code-redis.service` 和 `/etc/tt-code-redis.conf`，只监听 `127.0.0.1:6381`。
- SQLite：在数据盘现有 DB 做加法迁移。

## GitHub-first 门禁

1. 最终 diff 的全量 Python/Node/浏览器/静态检查全部通过。
2. P0/P1 代码复审关闭，工作树提交并 push；记录 exact GitHub SHA。
3. 服务器只从该 SHA 创建 `/opt/tt-post/releases/<sha>`；不得把未提交工作树当成 release。
4. 部署前重新读取线上 current symlink、主 app hash、服务/timer 状态和 DB/静态基线。若线上版本比预期更新，停止并先做语义 rebase。
5. secrets、生产 env 和 bearer 不进入 GitHub、命令回显、日志或文档。

## 部署前只读检查

### 主机和数据盘

```bash
hostname
findmnt -n -o TARGET,SOURCE,FSTYPE,UUID,OPTIONS /mnt/data-disk
df -h / /mnt/data-disk
test "$(findmnt -n -o UUID /mnt/data-disk)" = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
```

UUID、挂载或空间异常时停止；不得让 `/mnt/data-disk/...` 静默落到根盘目录。

### 版本和服务

记录：

- `readlink -f /opt/tt-post/current`
- `/root/drama_material_service/app.py` SHA-256
- `tt-post-service.service`、`drama-material-api.service`、Nginx MainPID/状态
- `tt-post-runner.timer`、`tt-post-prepare.timer` 状态；只读查看，不触发
- 当前 Nginx snippets 及 hash

### DB 和零发布基线

数据库固定为 `/mnt/data-disk/tt-post-publisher/tt-post.sqlite3`。用 readonly connection 记录：

- `PRAGMA integrity_check`
- queue 总数、max queue ID、非空 publish ID 数
- schedule run、random plan、event 等非敏感计数
- `tt_post_code_route` / `tt_post_code_recycle_audit` 是否已存在及行数
- 不打印 caption、long URL 或 token

### 静态基线

记录文件和公网无缓存 SHA-256：

- `/usr/share/nginx/html/tt-drama-search.html`
- `/usr/share/nginx/html/tt-drama-search.js`
- `/usr/share/nginx/html/tt-post-pool.html`
- 原 `/tt` HTTP 状态、无 Location 和一次真实搜索主流程

## 备份

备份目录：

```text
/mnt/data-disk/tt-post-publisher/backups/<timestamp>-tt-code-pre-<old_sha>/
```

必须包含：

1. SQLite online backup `tt-post.sqlite3.pre`，并单独执行 `integrity_check=ok`。
2. 原 current symlink 目标和 release 存在性。
3. `/root/drama_material_service/app.py`。
4. 将变更的管理页、Nginx static、新文件存在性清单；原 `/tt` 两文件只备份/hash，不覆盖。
5. Nginx snippets、`/etc/tt-post.env`、`/etc/tt-post-app.env`、受影响 systemd unit；敏感文件保持原 owner/mode且不输出内容。
6. 相对路径 SHA-256 manifest，并在备份目录内复核。

## Redis 部署合同

仓库资产：`deploy/tt-code-redis.conf`、`deploy/tt-code-redis.service`。

实际配置：

- bind `127.0.0.1`，port `6381`，`protected-mode yes`
- 无 RDB/AOF；SQLite 才是恢复源
- data dir `/mnt/data-disk/tt-post-publisher/redis`，unit 启动前创建为 `tt-post:tt-post 0700`
- `maxmemory 128mb`、`allkeys-lru`
- 禁用 `FLUSHALL`、`FLUSHDB`、`CONFIG`
- systemd 限制网络为 `AF_UNIX AF_INET`，不开放安全组/防火墙公网端口

sidecar env 只有：

```text
TT_POST_CODE_REDIS_HOST=127.0.0.1
TT_POST_CODE_REDIS_PORT=6381
TT_POST_CODE_REDIS_TIMEOUT_SECONDS=0.2
```

主 app env 增加/确认：

```text
TT_POST_CODE_RESOLVER_TIMEOUT=3
```

正缓存 24 小时、负缓存 30 秒和随机 namespace 是当前代码常量，不配置 Redis DB、ACL/password、TTL 或 namespace env。

安装前在目标机验证 Redis 包版本、`redis-server`/`redis-cli` 路径和 `systemd-analyze verify`。启动后检查：

```bash
redis-cli -h 127.0.0.1 -p 6381 ping
ss -lnt '( sport = :6381 )'
```

输出不得出现 `0.0.0.0:6381`、`[::]:6381` 或公网地址。

## 候选 release 与 DB 副本演练

1. 从 exact SHA 创建候选 release并校验文件 hash/可 import。
2. 对 online backup 的第二份副本执行新 `ensure_storage()`，不接生产 DB。
3. 断言新增：`tt_post_code_route`、`tt_post_code_recycle_audit`、两个 route index、状态 trigger、queue.code。
4. 断言旧表/旧行计数不变、重复迁移幂等、故障注入完整回滚、`integrity_check=ok`。
5. 用隔离 Redis/fake resolver运行 code、clone/fallback、陈旧 namespace 和慢 Redis 锁边界测试。
6. 候选目录运行与本地相同的全量 TT 命令。

## 生产部署步骤

1. 再次确认 exact SHA、backup manifest、数据盘和零真实发布边界。
2. 安装 Redis 软件包；安装 config/unit，`daemon-reload`，启动并 enable 独立 6381，验证 PING/loopback。
3. 以保留 owner/mode 的原子方式更新 `/etc/tt-post.env` 和 `/etc/tt-post-app.env`；不输出 secrets。
4. 准备新 Nginx snippet 和静态文件，先在临时位置检查；不得写旧 `/tt` 两文件。
5. 原子切换 `/opt/tt-post/current` 到 exact release。
6. 从同一 exact SHA 安装主 app `app.py`、更新后的 `tt-post-pool.html`、新 code page/JS 和 Nginx snippet。
7. 执行 `nginx -t`；失败立即停止，不 reload。
8. 重启 `tt-post-service.service` 和 `drama-material-api.service`，只 reload Nginx。不要重启 GPU，不要触发 runner/prepare。

## 上线验证

### 服务和权限

- sidecar `127.0.0.1:18829/health` 正常。
- sidecar internal resolver 无 bearer 返回 403；带 bearer 只在服务器内部验证且不得打印 token。
- 主 app 8787 health/既有 API 正常。
- Nginx `-t` 通过；`/tt-code` 与新 JS 200/no-store。
- Redis PING 正常且只监听 127.0.0.1:6381。

### 公共 API

- 未知四位 code 返回 404 `tt_code_not_found`，不为测试写 route。
- 选择生产已存在且无 published route 的安全剧 ID，验证 `generic_fallback` 和 Search/Featured channel；不存在剧 ID 返回 404。
- 只有生产已经存在可用 published route 时才验证 clone/code exact；不得伪造 published row 或触发发布。
- 检查响应是一条组合 item，包含剧目元数据和 route，不再产生前端第二次 drama resolve。
- 检查 target host/path/`af_dp`/参数集合；code exact channel TT，ID/Featured channel 对应 source。

### Redis 降级

1. 公共查询后确认 Redis 有缓存活动（只记 DBSIZE/统计，不输出 key/value）。
2. 临时停止独立 Redis，重复同一安全 GET，确认由 SQLite 返回相同业务结果。
3. 立即恢复 Redis并确认健康。该动作只影响可丢弃 cache，不改变 SQLite。

### 页面和旧 `/tt`

- 390x844 和桌面视口：恰好五张卡、按钮、鼠标/触控笔 drag、snap、拖动零 resolver、轻点一次请求。
- code 输入自动大写；invalid/404/503 fail closed且无 console error。
- 对照部署前 hash，原 `/tt` HTML/JS 文件和公网主流程必须完全一致。

### DB 与零发布

- 迁移后 `integrity_check=ok`，新增表/字段/trigger/索引正确。
- 若上线前 route/audit 为 0，验证后仍应为 0；本验收不创建正式 queue。
- 对比 queue、max ID、publish ID、run 等基线；自然 scheduler 的无关变化单独标识，不能当作本需求测试。
- 日志中不得出现 publish/canary/run-now、内部 token、Redis key/value或完整 SQL/堆栈。

## 回滚

1. 保存失败 release 的服务状态和非敏感日志。
2. current symlink 切回部署前 release。
3. 恢复备份的主 `app.py`、管理页、Nginx snippets、env 和相关 unit。
4. 删除/下线新增 `/tt-code` HTML/JS 与 snippet；原 `/tt` 恢复/保持原 hash。
5. 重启受影响 sidecar/API，`nginx -t` 后 reload。
6. 停止/disable 独立 Redis；清理缓存不会影响 SQLite。
7. 默认保留加法 route/audit 表、trigger 和 queue.code，不 DROP、不恢复整库；旧代码应忽略它们。

只有确认数据库损坏且得到明确批准，才停止所有 writer、保存损坏副本并按 manifest 恢复 online backup。普通代码回滚绝不能用旧 DB 覆盖可能已经前进的 queue/publish ledger。

## 上线后补录

```text
GitHub commit:
old release:
new release:
backup directory / manifest:
old /tt hashes before / after:
new /tt-code HTML / JS hashes:
main app hash before / after:
Redis package / unit / listen / PING:
DB baseline / migration / integrity:
Nginx test and service PIDs:
API and browser evidence:
zero-publish ledger evidence:
rollback point / rehearsal:
```

## 观察项

- code 分配碰撞、位图 fallback、总占用率、`tt_post_code_recycle_audit`。
- Redis 连接/超时/内存、sidecar SQLite fallback；当前公共层不暴露 code-cache 命中 header。
- 公共 API 2xx/4xx/5xx、p95、限流/overloaded 和 DramaWave 上游错误。
- `/tt` 与 `/tt-code` 可用性、Featured 五条完整率。
- 不以监控为由触发真实 TikTok 发布。
