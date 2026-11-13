# Moboreels 收益归因、社媒宏与工具部署

本目录区分已准备、已安装、已激活、业务验收通过。由 root 审阅最终 GitHub SHA 后执行；不重发社媒，不重置队列，不覆盖订单、推广与短链数据。

## 只读预检

2026-11-13 11:25:26 北京时间：63350 经现有 SQL FIFO 返回 ads_ai、read_only=1；订单、广告收益、广告日版本、短链、媒体账号报备五表均不存在。数据盘 UUID 为 3e8ac4e8-7770-456d-9e89-2ec5dd405fa8；同轮较早检查可用 135.77 GiB。

完整元数据在服务器 /mnt/data-disk/moboreels-library/deployments/attribution-preflight-20261113T032526Z/schema-before.json，0600 文件、0700 目录。core sync/promotion 无运行中任务；X、YouTube 有效执行租约为 0；TT coordinator 18、TT Auto 16；FB coordinator 3、FB video 2。FB 未决 native operation 1、claim 回执 135 仅留作审计，不是清锁或重发依据。切换前必须重查这些即时状态。

YouTube 准备与发布共用 /root/drama_material_service/data/drama_material_jobs.sqlite3。历史 uploading、review、scheduled、queued 状态不能代替有效租约判断。

## 版本、目录与凭据

开发基线 49794d74，原 sidecar 为 1742279d5ad07aaa0df5e5f86a49d9ccf6348e0b。原 main app.py SHA256 为 c03c4fe947a04d45e2b25f333f2e5192252e19001bd5c31076bfa0a7743c8472。真实 owner 文件及 current 链接以 baseline-manifest.json 为准。

main 仅安装 Git diff 的实际改动；已有文件没有基线则中止。owner 各自完整复制原源码目录再覆盖自身变更，保留不同 lane 代码差异。候选放 /mnt/data-disk/moboreels-social-releases/<SHA>/<deployment-id>/<lane>，父目录与新源码目录 0755。不得放在 0700 的 Moboreels 数据根目录下。manifest、备份、证据继续保存在受限数据目录。

/etc/moboreels-library.env 保持 root-only。新 /etc/moboreels-macro.env 仅有随机 macro token 和机构 ID，通过 ACL 仅授权 x-post-automation、x-post-daily、fb-auto-post、tt-post 读取。凭据不进入 Git、日志或终端输出。

## 精确 GitHub 源码与 stage

先将最终代码推送 GitHub，再在服务器 fetch 并建立 detached checkout。可以先用 <SHA>.stage 准备，但必须在执行 stage 前定为最终 release 路径；manifest 绑定绝对路径，随后不得移动。

~~~sh
SHA=<40-character-pushed-commit>
RELEASE=/mnt/data-disk/moboreels-library/releases/$SHA
git -C /mnt/data-disk/codex/workspaces/moboreels-library-src fetch origin codex/moboreels-attribution-20261113
git -C /mnt/data-disk/codex/workspaces/moboreels-library-src worktree add --detach "$RELEASE" "$SHA"
/usr/bin/python3 "$RELEASE/ops/moboreels-attribution-20261113/deploy.py" "$SHA" stage
~~~

stage 核对 remote、tracked 工作树、精确 SHA、文件与 current 基线，建立候选，不建表、不切换、不重启。整棵原树/候选摘要防止覆盖并发修改，忽略 pycache。worker 仅新增 __init__、settings、macro_client 三份共享模块，不复制供应方模块或凭据。

保存输出的 DEPLOYMENT；审阅 manifest 全部 target/source/before/after/phase。复制 activation-checks.template.json 到 DEPLOYMENT/private/checks.json，填入完整 SHA 与各 lane 暂停前 scheduler snapshot 路径；占位值不能执行。

## Schema 与 core

~~~sh
/usr/bin/python3 "$RELEASE/ops/moboreels-attribution-20261113/deploy.py" "$SHA" schema --deployment "$DEPLOYMENT"
/usr/bin/python3 "$RELEASE/ops/moboreels-attribution-20261113/deploy.py" "$SHA" core --deployment "$DEPLOYMENT" --checks "$DEPLOYMENT/private/checks.json"
~~~

schema 只执行三份审阅 DDL 的固定五表 CREATE TABLE。63353 只用于该 DDL 与连接身份校验；表、列、索引、中文备注读回全部走 63350/FIFO。同名表已存在或部分创建后重跑会停止，先核对 schema-intent/before/after，不能用 IF NOT EXISTS 掩盖漂移。

core 更新 sidecar、Moboreels 静态页、合并导航、Nginx、report service/timer、受限 macro 配置；只重启 moboreels-library、reload nginx。main app.py 最后切换，因此新主站接口在 main 激活后开放。核对原 catalog timer 仍每日 Asia/Shanghai 09:00，report timer 为每日 10:00，下一次触发用 systemctl list-timers/show 和 systemd-analyze calendar 交叉检查。保留 /m/ 路由与页面 no-store。

## 排空与逐 lane 激活

顺序 core → x/fb/tt/tt_auto → main。TT 与 TT Auto 共用 tt coordinator 开关，两阶段完成前保持暂停。最后才安装 app.py 与社媒界面，避免新 TT 可信身份字段先于 owner 代码到达。

1. 用现有 scheduler health 工具在暂停前保存当前 lane 原始状态。
2. 从 coordinator live Store 读取原 config version、该 lane enabled、preparation_only。仅用 Store.channel_enabled(lane, False) 暂停该 lane 新 admission/claim，并记录修改后的版本；不用 cutover.pause，不改队列，不停通用 CPU node。
3. 只暂停 checks 列出的 timer/path 触发器；让正在执行的 oneshot 自然结束。不要 stop 活跃 runner/prepare/reconcile，也避免在切换期间新增本 lane 手动发布请求。
4. 等 coordinator 非终态有效租约和 owner busy 归零。未知结果保持原状。
5. 脚本在线备份 SQLite、quick_check、复核 busy/SHA，才切换与窄重启。
6. scheduler restore 恢复原本活跃的依赖，保留原暂停状态。TT 第一阶段延迟 restore，TT Auto 完成后用同一暂停前快照恢复。
7. 核对 coordinator 版本和 lane 状态，仅恢复本轮原本 enabled 且由本轮暂停的 lane，保持原 preparation_only。遇外部版本变动先核对，不盲目启用。

~~~sh
/usr/bin/python3 /opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py snapshot --lane x --output "$DEPLOYMENT/private/x-before-pause.json"
/usr/bin/python3 "$RELEASE/ops/moboreels-attribution-20261113/deploy.py" "$SHA" x --deployment "$DEPLOYMENT" --checks "$DEPLOYMENT/private/checks.json"
~~~

X 重启 x-post-automation、x-auto-post-service；FB 重启 fb-auto-post-service；TT 重启 tt-post-service；TT Auto 重启 tt-auto-post-service。Requires 影响的依赖由 scheduler restore 验证恢复。main 只重启 drama-material-api、youtube-auto-publish-worker、pipeline-youtube-canonical，先排空 YouTube lane。

脚本不会自行暂停/恢复 coordinator，执行者必须记录并完成。不能以父 HTTP 存活代替 scheduler 验收。真实报告与验证短链使用 acceptance.py，不发布任何社媒内容。

## 失败恢复与回滚

manifest 在 live mutation 前记 installing，成功记 activated，异常记 needs_reconciliation。核对文件、路径、进程与持久化记录后，同 SHA/DEPLOYMENT 加 --resume 恢复未完成 phase，原备份不覆盖。schema 部分执行单独核对，不盲目重试。

切换前备份/复查失败会恢复保存的 scheduler 基线；coordinator 仍由执行者按本轮记录恢复。失败不自动回退 sidecar，防止已返回的短链失效。

~~~sh
/usr/bin/python3 "$RELEASE/ops/moboreels-attribution-20261113/rollback.py" "$DEPLOYMENT" x --checks "$DEPLOYMENT/private/checks.json"
~~~

回滚 checks key 为 rollback-x/fb/tt/tt_auto/main，按同样 busy/scheduler 规则填入。默认只退社媒代码与 current 链接，保留 sidecar、/m/、macro 配置、五表、所有 SQLite 与回执。core-ui 只退界面/导航，仍保留短链服务。

不可将 SQLite 备份覆盖回活跃账本，不可 DROP 新表或删除成功短链。需回退 sidecar 时，应部署兼容现有短链/schema 的修复版本，不能使已发布 /m/ 返回 404。
