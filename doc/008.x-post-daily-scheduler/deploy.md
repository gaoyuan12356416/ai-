# 部署文档

## 变更内容

- 部署 X 日批次/全局去重/日志查询 release。
- 增量迁移 X Post SQLite。
- 部署管理员日志页面和导航。
- 安装 `x-post-daily.service` / `x-post-daily.timer`，默认北京时间 10:00。

## 配置项

- `/etc/x-post-automation.env` 为 root-owned `0600`；`/etc/x-post-daily.env` 为 root-owned `0400`，均由 systemd 在降权前注入。
- Sidecar 使用专用用户 `x-post-automation`；runner 使用不同的 `x-post-daily`，后者不可读 SQLite、Token 目录或 OAuth env。
- 精确 release 位于 `/opt/x-post-automation/releases/<sha>`，稳定链接 `/opt/x-post-automation/current`；76MiB 静态 ffprobe 安装到数据盘 `/mnt/data-disk/x-post-automation/bin/ffprobe`（root:root `0555`），不占根盘。
- `/mnt/data-disk/x-post-automation/s2l` 归 `x-post-automation`、模式 `0755`（供 Nginx 只读）；`media-work` 归 `x-post-automation`、模式 `0700`。两者必须由部署预建，运行时禁止在挂载消失后自动创建。
- `/mnt/data-disk/x-post-automation/daily-work` 归 `x-post-daily` 独占 `0700`，预检媒体禁止落根盘 `/tmp`。
- `/etc/x-post-daily.env` 只含只读 MySQL 63350、专用 daily loopback bearer、固定三个账号 ID、候选上限、开始日期；该 bearer 必须与 Sidecar 后台管理 bearer 不同，不能回退复用。
- `/etc/x-post-automation.env` 同时声明专用 daily bearer 和同一组三账号 ID，供 Sidecar 服务端执行路由/账号范围校验。
- `X_POST_DAILY_ACCOUNT_IDS` 必须恰好三个不同正整数。
- `X_POST_DAILY_START_DATE` 首次部署设置为次日。

## 数据库变更

- `x_post_daily_run` 新表。
- `x_post_queue` 增量列与唯一索引。
- 迁移前对生产副本运行重复检查；任何冲突中止部署。
- 发布成功后回滚只回代码/unit，不删除新表或恢复旧数据库覆盖真实日志。

## 部署步骤

1. 验证代码/Skill 两个工作树状态和 GitHub 精确 commit。
2. 验证 `/mnt/data-disk` 是真实挂载点、UUID/空间/权限正确；创建两个 system user、`/opt` release/bin，并显式预建数据盘 `s2l`、`media-work`、`daily-work`：`s2l=x-post-automation:0755`、`media-work=x-post-automation:0700`、`daily-work=x-post-daily:0700`。
3. 在线备份 SQLite，备份 Token 目录 hash/mode、env、unit、Nginx/静态页面、当前 release 和 timer 状态。
4. 在备份数据库副本迁移并运行全部测试。
5. 从 GitHub checkout 精确 commit 到 `/opt/x-post-automation/releases/<sha>`，验证 hash、Python 3.9 和全部测试；复制已校验静态 ffprobe 到数据盘只读 `bin/`。
6. 先在 SQLite 备份副本验证迁移；再停止 Sidecar，原子备份/调整 `/var/lib/x-post-automation` 为专用用户，生成互不相同的 backend/daily bearer，切换 release，运行 `systemd-analyze verify` 后只重启 `x-post-automation.service`。主 API 只有在 live composite 基线精确匹配时才部署和窄重启。
7. 部署静态日志页/导航，验证管理员鉴权和 no-store。
8. 安装 timer/service，先验证服务用户不能读取另一方 Token/env，再调用 storage preflight 和独立只读 selector/媒体预检核对候选；用次日 `start_date` 门禁验证 oneshot 不会发帖，最后 `enable --now` timer。
9. 核对下一次触发时间、服务状态、journal 脱敏和 DB 唯一约束。

## 验证步骤

- 全量 unittest、py_compile、JS syntax、`git diff --check`。
- local/public Sidecar health 200，公网 internal 404。
- 管理员日志页面/API 200；普通用户/API Token 403。
- 生产副本与 live 迁移后旧行/Token hash/mode 不变。
- `systemd-analyze verify` 无未知指令；两服务均非 root、capability 空、`ProtectHome=yes`。
- daily 用户不能读取 `/var/lib/x-post-automation/tokens`；ffprobe 子进程不继承 X/MySQL 环境。
- root:root `0600` Sidecar env 由 systemd 注入后，非 root Sidecar 可启动且不尝试读取秘密文件；backend/daily bearer 不相等。
- 在恶意 `http_proxy/https_proxy` 环境下，runner、后台 loopback client 和 health waiter 仍直连 127.0.0.1；bearer 不进入代理。
- daily bearer 对 canary/authorize/通用 accounts/logs/runs 返回 403，仅能校验固定三账号、创建固定计划、发布其正式 queue。
- storage preflight 确认 mount/固定 `s2l`/`media-work` 目录、设备身份、原子写和至少三份最大候选空间；`daily-work` 位于数据盘。
- timer active/waiting，next trigger 为次日 10:00 CST；部署日补跑被 start_date 拦截。
- 独立只读预检不创建 run/queue/short link/Post；部署日手工运行 oneshot 返回 `skipped_before_start_date`。

## 回滚方案

1. `systemctl disable --now x-post-daily.timer`，停止新调度。
2. 保留新日志/短链/当前 Token；将稳定 release 链接切回上一个精确 commit，恢复 unit/env ownership/静态备份并窄重启。
3. 已产生真实日志后不恢复部署前 SQLite，不删除 `x_post_daily_run`/queue/log。
4. 恢复静态页面/导航时保留现有 Post 的 `/s2l/<log_id>.html`。
5. 若首次调度尚未发生且迁移无真实新记录，可在人工核对后用在线备份回滚数据库。

## 注意事项

- 不在部署时手工执行三账号真实发布；首个正式批次由次日自然 timer 触发。
- timer 启用前必须确认三个账号当前 active/publish eligible。
- 不输出或提交任何真实密码、OAuth Token 或内部 bearer。
