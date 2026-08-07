# 部署与回滚

## 部署原则

- 只部署 GitHub 上已通过评审的精确提交，不在生产服务器手改源码。
- 旧 `tt-post-service.service`、旧 TT 发布池页面、API、SQLite 和调度保持原样。
- 新系统首次上线只验证“关闭默认”：三个真实发布门禁全部为 `0`，模板库为空，不创建 run/task，不触发真实 TT 发布。
- 调度与执行分离：`tt-auto-post-scheduler` 每分钟只执行 `--mode tick`；`tt-auto-post-runner` 通过 timer/path 执行 `--mode execute`。GPU prepare 较慢时不得阻塞后续计划时刻入队。

## 部署前

1. 复跑 117 个新系统测试、64 个旧 TT 回归、JS 语法检查、浏览器无发布验收和最终安全复核。
2. 提交并推送 `codex/tt-auto-publish-templates-20260805`，记录提交 SHA；确认生产当前提交是目标提交的可安全升级祖先，且工作区没有未提交改动。
3. 只读确认 `/mnt/data-disk` 为独立挂载、`127.0.0.1:18831` 未占用、现有 GPU 回环 `127.0.0.1:18830` 可达，旧 `tt-post-service.service` 为 active。
4. 备份当前主 API release/软链、静态文件、systemd 单元与 drop-in、两个站点的 Nginx 配置，以及已有的新系统 SQLite、公开短链目录和日志；记录备份绝对路径与 SHA256。
5. 创建独立内部 bearer，不得复用旧 `TT_POST_INTERNAL_TOKEN` 或 `TT_POST_GPU_INTERNAL_TOKEN`，也不得在命令输出或日志中打印真实值。

## 安装目录与配置

1. 从 GitHub 检出精确 SHA 到 `/opt/tt-auto-post/releases/<sha>`，原子切换 `/opt/tt-auto-post/current`。
2. 安装配置文件：
   - `/etc/tt-auto-post.env`：复制 `deploy/tt-auto-post.env.example`，`root:root 0400`。
   - `/etc/tt-auto-post.secrets`：复制 `deploy/tt-auto-post.secrets.example` 并写入独立 token，`root:tt-post 0440`。
   - 主 API 使用 `deploy/tt-auto-post-app.env.example` 对应的 drop-in；其 token 必须与新 sidecar 一致。
3. 明确检查以下三项均为 `0`：

   ```text
   TT_AUTO_POST_LIVE_ENABLED=0
   TT_AUTO_POST_DIRECT_AUDIT_APPROVED=0
   TT_AUTO_POST_URL_PROPERTY_VERIFIED=0
   ```

4. 安装 `deploy/tt-auto-post-tmpfiles.conf` 为 `/etc/tmpfiles.d/tt-auto-post.conf`，然后执行：

   ```bash
   systemd-tmpfiles --create /etc/tmpfiles.d/tt-auto-post.conf
   ```

   验证私有状态目录 `/mnt/data-disk/tt-auto-post-publisher` 为 `0700 tt-post:tt-post`；Nginx 只需遍历的 `/mnt/data-disk/tt-auto-post-public/s2l/tt-auto` 路径为 `0755 tt-post:tt-post`。私有 SQLite 不能放到公开目录。

5. 安装并 `systemctl daemon-reload`：
   - `tt-auto-post-service.service`：loopback `127.0.0.1:18831` sidecar。
   - `tt-auto-code-route.service`：loopback `127.0.0.1:18832` 四位码 broker；普通自动发布 sidecar 对旧 SQLite 仍为只读，只有该 broker 可写共享码路由表。
   - `tt-auto-post-scheduler.service/.timer`：每分钟生成到期 run，`--mode tick`。
   - `tt-auto-post-runner.service/.timer/.path`：并发执行已入队账号任务，`--mode execute`；path 用于手动请求唤醒。
   - `tt-auto-post-metric.service/.timer`：按完整北京时间日刷新指标缓存。

## 指标首次回填

在任何模板可能启用之前，使用生产同一组 EnvironmentFile 做一次最近 30 个完整北京时间日回填。不要把凭据展开到命令行或终端输出：

```bash
systemd-run --unit=tt-auto-post-metric-backfill-30d \
  --wait --collect \
  --property=User=tt-post \
  --property=Group=tt-post \
  --property=WorkingDirectory=/opt/tt-auto-post/current \
  --property=EnvironmentFile=/etc/tt-auto-post.env \
  --property=EnvironmentFile=/etc/tt-auto-post.secrets \
  --property=EnvironmentFile=/etc/tt-post.secrets \
  /usr/bin/python3 /opt/tt-auto-post/current/scripts/tt_auto_post_metric_runner.py --lookback-days 30
```

首次回填前必须使用生产同版本 MySQL 的只读连接对日指标 SQL 执行
`EXPLAIN`。生产 MySQL 5.7 开启 `ONLY_FULL_GROUP_BY` 时，排序表达式必须与
分组表达式一致；出现 errno 1055 时不得启用指标 timer 或模板。

回填失败不得启用模板；确认每个完成日都存在 READY active generation，失败 generation 没有切换 active。日常 timer 继续按 `TT_AUTO_POST_METRIC_LOOKBACK_DAYS=7` 保温默认窗口。

日常指标定时器在每小时 `:12` 刷新。北京时间 `00:00` 至当日首轮刷新完成前，包含“昨天”这一新完整日的窗口尚未 READY；此时选择会 fail-closed 进入 `retry_wait`，在刷新完成后的下一次重试继续，不会降级使用不完整日或旧窗口。因此设置在午夜前 12 分钟内的模板可能延后执行，属于可预期等待而非终态失败；若业务要求更贴近计划时刻，应把模板时间设在 `00:20` 以后。

## 启动与 Nginx

1. 启动新 sidecar并验证 loopback health；随后切换包含独立代理的主 API release。不要停止或重启旧 TT sidecar。
2. 安装 `deploy/nginx-tt-auto-publish.conf` 到 `ai.yingliangads.com` TLS server，安装 `deploy/nginx-tt-auto-short-domain-location.conf` 到 `gy.g2flow.com` 且位于更宽泛的 `/s2l` location 之前。
3. 执行 `nginx -t` 成功后 reload；管理页面/API 必须 no-store，短链只允许匹配 `/s2l/tt-auto/<task_id>.html` 的 GET。
4. 启用并启动 `tt-auto-post-scheduler.timer`、`tt-auto-post-runner.timer`、`tt-auto-post-runner.path`、`tt-auto-post-metric.timer`。模板为空时 tick/execute 必须无副作用。

## 发布后关闭默认验收

- `tt-post-service.service` 仍为原 release 且 active；旧 TT 页面和 API 回归正常。
- 新 sidecar只监听 `127.0.0.1:18831`，四位码 broker 只监听 `127.0.0.1:18832`；主 API 通过同源代理访问管理 sidecar，外部不能直连任一内部接口。
- `systemctl list-timers` 可见 scheduler、runner 和 metric；scheduler 与 runner 使用不同锁，worker 并发数有界。
- 三个发布门禁均为 `0`；模板数量为 0；run/task 数量为 0；material ledger 没有由本次验收新增的冻结记录。
- 仅打开模板列表、创建页和运行列表，验证空态、权限和 no-store；不在生产创建模板、不点击立即执行、不做真实 canary。
- 日志不含 bearer、MySQL/GPU 凭据、源素材 URL、准备后 URL或未脱敏的下游响应。

## 回滚

1. 先停止并禁用 `tt-auto-post-scheduler.timer`，再把主 API 回切上一 release，阻断“立即执行”和模板写入入口；此时必须继续运行 `tt-auto-post-metric.timer`、`tt-auto-post-runner.timer/.path` 与 `tt-auto-post-service.service`，不得先关闭三重门禁、指标刷新或 sidecar。这样午夜窗口内等待 READY 指标日的任务仍能收敛。
2. 使用只读查询持续检查独立账本，等待所有已创建账号任务进入终态：

   ```bash
   sqlite3 -readonly /mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3 \
     "SELECT status, COUNT(*) FROM tt_auto_task WHERE status NOT IN ('no_candidate','published','failed','canceled','skipped') GROUP BY status ORDER BY status;"
   ```

   查询必须返回 0 行。尤其 `publishing`、`unknown`、`reconciling` 必须取得明确远端结果并完成 reconcile；若任一状态长期无法收敛，回滚视为被阻断，保留 runner/sidecar 和短链服务并升级人工处理，禁止通过停服务掩盖未知发布结果。
3. 账本已排空后停止并禁用 `tt-auto-post-runner.timer`、`tt-auto-post-runner.path` 和 `tt-auto-post-metric.timer`，等待当前 oneshot 完整退出，再次执行上面的只读查询确认仍为 0 行，然后依次停止 `tt-auto-post-service.service` 与 `tt-auto-code-route.service`。sidecar 的 graceful shutdown 会等待正在处理的 HTTP 工作线程。
4. 验证主 API 已无新管理入口，旧 TT 服务和页面正常；随后可关闭三重门禁。以下发布后事实必须永久保留：
   - `/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3` 及素材永久 ledger/事件审计；
   - `/mnt/data-disk/tt-auto-post-public/s2l/tt-auto/` 下所有已生成短链文件；
   - `gy.g2flow.com` 的 `/s2l/tt-auto/<task_id>.html` Nginx location 和访问能力。
5. 不得因应用回滚删除、改写或重新分配既有 task ID 短链及 `7_000_000_000_000_000_000 + task_id` 对应的四位码路由；已发布 TT 帖子依赖这些不可变 URL/码。恢复 Nginx 备份时必须把该短链 location 合并保留并先执行 `nginx -t`。
6. 回滚不修改或迁移旧 TT 发布池数据，也不释放新系统已冻结素材。记录停止时间、最后 run/task 状态和保留目录校验值，供后续 reconcile/审计。

## 2026-08-06 发布文案剧 ID 宏可选增量部署

- GitHub 代码提交：`62e2f0b0e051ef875bdeb4237e9101688a3a600e`；组合基线同时包含通用渲染器修复提交 `837abc9` 和原生产自动模板提交 `bac0885`。
- 生产 release：`/opt/tt-auto-post/releases/62e2f0b0e051ef875bdeb4237e9101688a3a600e`；原 release：`/opt/tt-auto-post/releases/bac0885bec894b6d66d6bb5fdf0a81b7478b43f6`。
- 切换前备份：`/mnt/data-disk/tt-auto-post-deploy/backups/20260806-160626-caption-optional-pre`。`SHA256SUMS`、在线 SQLite 备份 `quick_check`、原 release/静态文件/systemd/门禁元数据均已校验。
- 部署仅切换 `tt-auto-post-service.service` 的不可变 release，并原子替换 `/root/drama_material_service/static` 与 `/usr/share/nginx/html` 下两个自动模板页面文件；未重启主 API、Nginx 或旧 `tt-post-service.service`。
- 新 sidecar `GET /health` 返回 200；公开 HTML/JS 返回 200，SHA-256 分别为 `df54ed403847bf3d7257ff665fea945925b6208a39aa511f4be01647ec042b94`、`93354fb3e8d80e8cc1fe764e527aea2185cdcf99435891a9746328d157bdc429`。
- 生产 release 上的只读验证确认不含剧 ID 宏、仅含 `{desc}` / `{url}` 的模板可通过归一化；未创建生产模板。三重门禁保持 0，模板/run/task/material ledger 均为 0，SQLite `quick_check=ok`。
- 旧 TT release 保持 `4362f3928e8c5c3f437917585b9f645e51986536`，PID `3055551` 未变化。scheduler、runner、runner path、metric timer 及新 sidecar 全部 active。
- `16:15` 的 scheduler/runner 自然触发恰好与 sidecar 重启窗口重叠，各失败 1 次；`16:16` 至 `16:19` 连续 4 轮自然触发均成功，最终 `Result=success`、`ExecMainStatus=0`，且账本仍为空，未产生发布副作用。
- 首次切换验收误用了不存在的 `/healthz`，因得到非 200 按预案自动回滚，且未替换页面或写入账本；确认正确健康路径为 `/health` 后重新切换并通过。该探针错误不是应用启动失败。

当前精确回滚必须先按上文账本检查确认无非终态任务，再执行：

```bash
ln -s /opt/tt-auto-post/releases/bac0885bec894b6d66d6bb5fdf0a81b7478b43f6 /opt/tt-auto-post/current.rollback-caption-optional
mv -Tf /opt/tt-auto-post/current.rollback-caption-optional /opt/tt-auto-post/current
install -m 0644 /mnt/data-disk/tt-auto-post-deploy/backups/20260806-160626-caption-optional-pre/root-static/tt-auto-publish-template.html /root/drama_material_service/static/tt-auto-publish-template.html
install -m 0644 /mnt/data-disk/tt-auto-post-deploy/backups/20260806-160626-caption-optional-pre/root-static/tt-auto-publish-template.js /root/drama_material_service/static/tt-auto-publish-template.js
install -m 0644 /mnt/data-disk/tt-auto-post-deploy/backups/20260806-160626-caption-optional-pre/published-static/tt-auto-publish-template.html /usr/share/nginx/html/tt-auto-publish-template.html
install -m 0644 /mnt/data-disk/tt-auto-post-deploy/backups/20260806-160626-caption-optional-pre/published-static/tt-auto-publish-template.js /usr/share/nginx/html/tt-auto-publish-template.js
systemctl restart tt-auto-post-service.service
curl -fsS http://127.0.0.1:18831/health
```

## 2026-08-06 `{code}` 四位码增量部署

- GitHub 代码提交：`5b18d1ef68614ae01bf97a7e092bcd0d9c345d3f`；生产 release：`/opt/tt-auto-post/releases/5b18d1ef68614ae01bf97a7e092bcd0d9c345d3f`；原 release：`/opt/tt-auto-post/releases/62e2f0b0e051ef875bdeb4237e9101688a3a600e`。
- 切换前备份：`/mnt/data-disk/tt-auto-post-deploy/backups/20260806-164548-code-macro-pre`。备份包含自动/旧 TT 两个 SQLite 的在线副本、环境文件、受影响 systemd 单元、页面、release/PID/门禁元数据和通过校验的 `SHA256SUMS`；两个备份库 `quick_check=ok`。
- 新增 `tt-auto-code-route.service`，仅监听 `127.0.0.1:18832`，以单独 systemd 沙箱写共享 `tt_post_code_route`；普通 `tt-auto-post-service.service` 对 `/mnt/data-disk/tt-post-publisher` 仍为只读。自动任务使用 `7_000_000_000_000_000_000 + task_id` 作为共享路由身份，不写旧队列、素材或调度表。
- 自动发布模板校验已允许 `{code}`；任务在 GPU prepare 前冻结码、短链和最终 caption，任务 caption 设为一次写入不可变，发布重试继续复用相同 caption。模板不使用 `{code}` 时不调用 broker。
- Windows 全套 117/117 通过；Linux release 上按锁目录拆分为 113/113 加 runner 4/4 通过，Git blob 逐文件校验 814 个文件一致。旧 TT 页面/API 64/64、旧 TT core/service/pool 258/258 通过。
- 新 broker、自动发布 sidecar、scheduler/runner timers、runner path、metric timer 全部 active；broker `/health` 与自动 sidecar `/health` 均为 200。公开模板 HTML SHA-256 为 `54d6be53744d10404077e30731eb3cba2468734dd1f00680edd8b00678531946`，磁盘与 HTTPS 响应一致。
- 三重发布门禁保持 0，自动模板/run/task/material ledger 均为 0，共享码路由保持 52 行，自动高位命名空间仍为 0；验收未保存模板、未分配生产四位码、未调用 GPU/TikTok。
- 旧 `tt-post-service.service` 保持 release `4362f3928e8c5c3f437917585b9f645e51986536` 和 PID `3055551`；主 API PID `3062660` 未变化，未重启主 API、旧 TT、Nginx 或 GPU。
- 首次尝试因 root 用户的 `test -w` 不能用于只读 release 权限判断而在前置校验回滚；第二次尝试因 `Type=simple` broker 启动后的单次即时探针发生监听竞态而回滚。两次均恢复原 release、环境、单元和页面，未启动新自动代码。最终改为校验实际 `0444` 权限并有界轮询健康检查后成功。
- `16:51` 的 scheduler/runner 自然触发与最终 sidecar 重启窗口重叠，各失败 1 次；`16:52`、`16:53`、`16:54` 连续 3 轮自然触发均成功，最终 `Result=success`、`ExecMainStatus=0`，账本与自动码命名空间仍为空。重启期间 runner path 被依赖停止后已显式恢复为 `active (waiting)`。

当前精确回滚必须先按上文账本检查确认无非终态任务，再执行：

```bash
systemctl stop tt-auto-post-scheduler.timer
# 再次确认 publishing/unknown/reconciling 等非终态任务为 0
systemctl stop tt-auto-post-runner.timer tt-auto-post-runner.path
systemctl stop tt-auto-post-service.service
systemctl disable --now tt-auto-code-route.service
install -o root -g root -m 0400 /mnt/data-disk/tt-auto-post-deploy/backups/20260806-164548-code-macro-pre/config/tt-auto-post.env /etc/tt-auto-post.env
install -o root -g root -m 0644 /mnt/data-disk/tt-auto-post-deploy/backups/20260806-164548-code-macro-pre/systemd/tt-auto-post-service.service /etc/systemd/system/tt-auto-post-service.service
unlink /etc/systemd/system/tt-auto-code-route.service
ln -s /opt/tt-auto-post/releases/62e2f0b0e051ef875bdeb4237e9101688a3a600e /opt/tt-auto-post/current.rollback-code-macro
mv -Tf /opt/tt-auto-post/current.rollback-code-macro /opt/tt-auto-post/current
install -o root -g root -m 0644 /mnt/data-disk/tt-auto-post-deploy/backups/20260806-164548-code-macro-pre/root-static/tt-auto-publish-template.html /root/drama_material_service/static/tt-auto-publish-template.html
install -o root -g root -m 0644 /mnt/data-disk/tt-auto-post-deploy/backups/20260806-164548-code-macro-pre/published-static/tt-auto-publish-template.html /usr/share/nginx/html/tt-auto-publish-template.html
systemctl daemon-reload
systemctl restart tt-auto-post-service.service
systemctl start tt-auto-post-scheduler.timer tt-auto-post-runner.timer tt-auto-post-runner.path tt-auto-post-metric.timer
curl -fsS http://127.0.0.1:18831/health
```

回滚只停止新的 broker，不删除已经分配的 `tt_post_code_route` 行；已进入发布文案的四位码必须永久保留可查询。

## 2026-08-06 生产内测真实任务增量部署

- 经用户再次明确确认后，约 `18:02` 将 `TT_AUTO_POST_LIVE_ENABLED`、`TT_AUTO_POST_DIRECT_AUDIT_APPROVED`、`TT_AUTO_POST_URL_PROPERTY_VERIFIED` 全部保持为 `1`；模板 1 仍为停用，仅手动任务进入执行链路，旧 TT 发布池继续运行。
- 切换前备份依次为：
  - `/mnt/data-disk/tt-auto-post-deploy/backups/20260806-180143-pre-live-internal-beta`
  - `/mnt/data-disk/tt-auto-post-deploy/backups/20260806-181712-pre-profile-alignment`
  - `/mnt/data-disk/tt-auto-post-deploy/backups/20260806-183411-pre-code-replay-fix`
  - `/mnt/data-disk/tt-auto-post-deploy/backups/20260806-190136-pre-caption-unicode-fix`
  最后一份备份包含新旧两个 SQLite 在线副本、当前环境和服务状态，两个库 `quick_check=ok`，`SHA256SUMS` 全部通过。
- GitHub 精确提交和部署顺序：
  - `eae6fcb9e666b53a3e89e6b576b33f47cfd7c286`：把账号快照外部身份规范化为旧池兼容的 W2A 用户名；
  - `3a8044d342d5c7bad8dc0d6472a04b7ea44fa667`：生产媒体 profile 对齐为 `tt-post-direct-outro-hevc-720x1280-v2`；
  - `2a1674d98cbb245bab0d4c0a6e83dbda092a6e69`：四位码 broker 对非 ASCII 归因字段使用 UTF-8 字节做恒定时间比较，使同一路由重试可幂等重放；
  - `3d1a33a2cb701bba49949c2243cdb5dddb50cf95`：任务状态库对冻结文案等不可变文本使用 UTF-8 字节比较，允许中文和 Emoji 文案在发布重试中通过幂等校验。
- 当前生产 release 为 `/opt/tt-auto-post/releases/3d1a33a2cb701bba49949c2243cdb5dddb50cf95`；自动 sidecar 与 broker 健康检查均为 200，三重门禁仍全部开启。旧 `tt-post-service.service` PID 始终为 `3055551`，未重启、未改代码或数据。
- 手动任务 1 在 GPU 前因 W2A 用户名格式失败；任务 2 在 GPU prepare 前因媒体 profile 不一致失败。两项均无 `publish_id`，对应问题已由上述 GitHub 提交修复。
- 手动任务 3 固定账号 640、素材 `6013146`、`content_id=peKST2RMpC`、四位码 `Q66Y`，GPU 成片已准备完成（13,019,687 bytes，96.767 秒）。同一路由在新 broker 上重放成功并保持原码，不新建任务、不重新准备素材。
- `18:47` 复核确认 SocialKit 源表中的 640、641、642 均为 `token_status=2/account_status=2`；640 和 642 已在 `18:30` 自动续期到次日，但 `ads_ai` 小时快照仍停留在 `18:05` 的旧到期值。`18:50` 手动执行现有 snapshot oneshot 成功同步 24 行后，640/642 均通过 5 分钟发布窗口；641 的 `disable_publish=1` 仍保持不可发布。此过程不需要重新授权，也未改源表。
- 任务 3 在账号快照恢复后暴露冻结中文/Emoji 文案的 Unicode 幂等比较缺陷；修复上线后继续同一任务，于 `2026-08-06 19:04:39 +08:00` 记录 `publish_id=v_pub_url~v2-1.7670872578680457224`，立即转入 reconcile-only，并于 `19:05:04` 确认为 `published`。run 3 为 `completed`，`unknown_outcome=0`。
- 四位码 `Q66Y` 同步进入 `published`；共享高位 route 未在旧 `tt_post_queue` 创建碰撞行。模板 1 仍停用，旧发布池和定时逻辑保持原样。

## 2026-08-07 账号快照滞后修复

- 保留 `is_active=1`、`account_status=2`、`token_status=2`、`disable_publish=0`、Token 非空和“到期时间晚于当前时间 5 分钟”全部安全条件，不删除校验。
- 仅当账号其他状态均正常、唯一不满足项为目标快照 Token 有效期窗口时，返回 `tt_account_snapshot_refresh_pending`；同一冻结任务、素材、GPU 成片和文案在 1 分钟后重试。其他瞬时失败仍按 5 分钟重试，已有 `publish_id` 或未知结果仍只允许 reconcile。
- SocialKit 账号快照 timer 从每小时 `:05` 改为每 5 分钟 `:02/:07/.../:57`。641 的 `disable_publish=1` 不会被误判为快照滞后，仍禁止发布。
- 旧 TT 发布池不切换 release、不重启服务；新自动发布三重生产闸门保持开启。本次部署前实时核验发现模板 1 已由现网状态开启，部署未改动该开关；本次未手工创建真实发布任务。
- 自动 sidecar 已切到 `/opt/tt-auto-post/releases/729ce90174e0c2c8fa1047295f6e606bf35cdb67`，健康检查为 200。旧 `tt-post-service.service` PID 部署前后均为 `3055551`。
- 部署后 `10:57/11:02/11:07` 三轮账号快照自然同步均成功；640、642 的目标快照与源端一致且可发布，641 仍因 `disable_publish=1` 不可发布。最终没有非终态自动任务，历史任务 3 仍为 `published`、原 `publish_id` 不变。

## 2026-08-07 原片直发 profile 对齐修复

- 自动发布是独立于旧素材池发布的 CPU consumer。切换 GPU 媒体模式时必须同时检查
  `/etc/tt-post.env` 和 `/etc/tt-auto-post.env`，不得只更新其中一个。
- 当前原片直发配置必须成对设置：

  ```text
  TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=0
  TT_POST_MEDIA_PROFILE_VERSION=tt-post-source-direct-v1
  ```

- 回切自动发布旧制作模式时也必须成对恢复：裁尾 `4.333333`，profile
  `tt-post-direct-outro-hevc-720x1280-v2`。旧制作代码保持不变。
- 部署前停止 `tt-auto-post-scheduler.timer`、`tt-auto-post-runner.timer` 和
  `tt-auto-post-runner.path`，确认两个 oneshot 均 inactive；创建 SQLite online backup，
  保存 env/unit/current release 及 SHA256。
- 本次失败 run 6 的 task 7–12 均为确定的 prepare 前失败，无 `publish_id`、无 publish
  attempt、无 unknown。task 6 留有 selection lease，恢复 runner 前必须在备份后终态化为
  failed，并把 run 6 收敛为 failed，防止 lease 到期后自动继续；不得重置或重跑 task 7–12。
- 切换 release/env 后，自动发布 `/health` 必须返回
  `profile=tt-post-source-direct-v1`、`source_trim_tail_seconds=0`；CPU tunnel 上的 GPU
  `/health` 必须返回相同 profile、`media_mode=source_direct`、`transition=none`。
- 验收只观察自然 scheduler/runner，不创建新 run，不调用 TikTok；真实测试由用户重新手动执行。

### 生产部署记录

- GitHub/生产提交：`4f2d3f7408fee1c7a2f8a37caf7081c821ee7bfd`。
- 新 release：`/opt/tt-auto-post/releases/4f2d3f7408fee1c7a2f8a37caf7081c821ee7bfd`；
  原 release：`/opt/tt-auto-post/releases/0392013f68825530ac52132c7be3c258650be1de`。
- 备份：`/mnt/data-disk/tt-auto-post-deploy/backups/20260807T181142+0800-source-direct-profile-pre-4f2d3f7`；
  SQLite online backup `integrity_check=ok`，`SHA256SUMS` 全部通过。
- GitHub archive SHA256：`841c97d5485ef43c6a89fcd26efeb557e45eb031863dae26c6745a9cfa45cd9e`。
- 候选 release compile 通过，自动发布定向测试 31/31 通过；本地完整 TT 回归 560/560 通过。
- task 6 在停止 sidecar/runner 后通过账本状态机终态化为 failed，错误码
  `source_direct_profile_mismatch_cancelled`；run 6 同步收敛为 failed。task 7–12 保持原失败事实，
  所有这些任务均无 `publish_id`、无 unknown、publish attempt 为 0。
- 自动发布 health 返回 `profile=tt-post-source-direct-v1`、trim `0`、三重门禁 open；GPU health
  返回相同 profile、`media_mode=source_direct`、`transition=none`、`direct_post_eligible=true`。
- 18:15、18:16 两轮自然 scheduler/runner 均 success；max run/task 仍为 `6/12`，publish_id
  仍为 `3`，unknown/nonterminal 均为 `0`，未调用 TikTok。

### 精确回滚

1. 停止 scheduler timer、runner timer/path，确认 scheduler/runner oneshot inactive；停止
   `tt-auto-post-service.service`。
2. 从上述备份恢复 `/etc/tt-auto-post.env`，把 `/opt/tt-auto-post/current` 原子切回
   `0392013f68825530ac52132c7be3c258650be1de`。
3. 重启自动发布 sidecar，验证旧 health；再恢复 scheduler/runner timer/path。
4. 常规代码/配置回滚不得恢复备份 SQLite：备份中的 task 6 仍是带有效 lease 的 selecting，恢复它会
   重新激活已关闭的测试任务。当前生产账本中的 failed 终态和事件审计必须保留。
