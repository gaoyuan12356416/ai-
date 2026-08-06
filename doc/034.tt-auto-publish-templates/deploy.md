# 部署与回滚

## 部署原则

- 只部署 GitHub 上已通过评审的精确提交，不在生产服务器手改源码。
- 旧 `tt-post-service.service`、旧 TT 发布池页面、API、SQLite 和调度保持原样。
- 新系统首次上线只验证“关闭默认”：三个真实发布门禁全部为 `0`，模板库为空，不创建 run/task，不触发真实 TT 发布。
- 调度与执行分离：`tt-auto-post-scheduler` 每分钟只执行 `--mode tick`；`tt-auto-post-runner` 通过 timer/path 执行 `--mode execute`。GPU prepare 较慢时不得阻塞后续计划时刻入队。

## 部署前

1. 复跑 108 个新系统测试、64 个旧 TT 回归、JS 语法检查、浏览器无发布验收和最终安全复核。
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
- 新 sidecar只监听 `127.0.0.1:18831`，主 API 通过同源代理访问，外部不能直连内部接口。
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
3. 账本已排空后停止并禁用 `tt-auto-post-runner.timer`、`tt-auto-post-runner.path` 和 `tt-auto-post-metric.timer`，等待当前 oneshot 完整退出，再次执行上面的只读查询确认仍为 0 行，然后停止 `tt-auto-post-service.service`。sidecar 的 graceful shutdown 会等待正在处理的 HTTP 工作线程。
4. 验证主 API 已无新管理入口，旧 TT 服务和页面正常；随后可关闭三重门禁。以下发布后事实必须永久保留：
   - `/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3` 及素材永久 ledger/事件审计；
   - `/mnt/data-disk/tt-auto-post-public/s2l/tt-auto/` 下所有已生成短链文件；
   - `gy.g2flow.com` 的 `/s2l/tt-auto/<task_id>.html` Nginx location 和访问能力。
5. 不得因应用回滚删除、改写或重新分配既有 task ID 短链；已发布 TT 帖子依赖这些不可变 URL。恢复 Nginx 备份时必须把该短链 location 合并保留并先执行 `nginx -t`。
6. 回滚不修改或迁移旧 TT 发布池数据，也不释放新系统已冻结素材。记录停止时间、最后 run/task 状态和保留目录校验值，供后续 reconcile/审计。
