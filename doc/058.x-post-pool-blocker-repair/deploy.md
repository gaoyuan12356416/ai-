# 部署与回滚

## 已发布代码

- CPU：43.166.187.96。
- GitHub分支：`codex/x-pool-blockers-20260828`。
- 生产提交：`814e168a48bbacae00384c0950576c5b253e21eb`。
- 当前release：`/mnt/data-disk/x-post-automation/releases/814e168a48bbacae00384c0950576c5b253e21eb`；`/opt/x-post-automation/current`已原子切换。
- 旧release：`/mnt/data-disk/x-post-automation/releases/18d9cfb68ee330db633b769112f0a50e38bc3e7c`。
- 备份：`/mnt/data-disk/x-post-automation/backups/20260828-pool-blockers-155312`。包含在线SQLite、切换前新快照、受控env、Token目录及主API共享代码。Token不入Git/报告，不自动回灌已轮换Token。

## 部署记录

1. 本地832项回归通过（Windows跳过2项）；新增配置解析的11项聚焦回归通过；最终生产提交在Linux通过832/832（38.593s）。
2. online backup quick_check=ok/FK=0；备份副本初始化后726 queue/726 log/171 relay/130 run/55 drama pool/841 material pool数量不变；识别账号8 locked、19 unknown，其余17个素材账号/16个短剧账号可用。
3. 主API共享service.py较旧，未发现其独立新行为。保留主APIapp/selector/OAuth，精确同步service.py和两个新helper，全部有备份及hash。
4. 外部操作曾于15:47:47暂停、15:59:58恢复三个timer。第一次部署门禁因状态变化拒绝，零代码/env变更。重查无inflight、代码/env/保护账本无漂移后，记录当前active状态，执行本次短暂维护暂停。
5. 16:11:04/05分别重启主API与Sidecar。Sidecar PID1777039工作目录指向目标release。配置独立开启deferred drama GPU repair：profile v5、repair timeout3600、schedule内部请求timeout7200；专用修复Token仅从服务器原受控配置复制。
6. 首次主API健康探测错误使用其不存在的/health得到404；未把404当成功。随后用真实/api/auth/status验证200及X admin接口401鉴权门禁，Sidecar/GPU /health均200。
7. 16:13:44健康及SQLite检查通过，保护queue/log/relay 533/719/726逐字段与备份一致，三个timer恢复部署前active状态。线上只读预检已返回精确409中文原因。

## 历史短剧恢复（独立阶段，未等同于代码上线）

- 清单：`deploy/recovery/x-post-drama-run-348-media.json`（635—647，13条）和`x-post-drama-run-350-media.json`（667—669，3条）。
- 先对备份副本完整媒体validate-only和apply演练；全部证明通过后，暂停原定时器、确认无在途X写、再次在线备份，再执行live validate-only/apply。
- 保留原target/relay/素材ID/内容/episode/绑定及queue/log主键；修复URL单独留原URL与不可变审计。仅重置已证明零尝试/无ID/无unknown的媒体失败。
- 不重建363、不清q533/719/726，不用真实测试帖验证。数据恢复只表示回到原队列待执行；真实发帖须以后续自然timer和ledger核对。
- 当前媒体校验/恢复进度见test-report.md；不得把本节计划步骤当成已经完成。

## 回滚

1. 停止三个timer，等待schedule/claim/manual服务退出，并查publish log无media_uploading/post_creating/repost_creating；不得强杀在途写。
2. 原子切回旧release，例如先建`/opt/x-post-automation/current.rollback`指向上述旧release，再用`mv -Tf`替换current。
3. 恢复备份中的`main-api-before-service.py`到主API的features/x_posts/service.py；两个新helper可保留为未使用文件，避免删除后续工作。
4. 恢复备份中x-post-automation.env和x-post-schedule.env至/etc，保留原属主权限；不要恢复Token或整库快照。
5. 窄重启drama-material-api.service、x-post-automation.service；复查/api/auth/status、X鉴权、Sidecar /health、commit/hash与SQLite。
6. 若历史恢复已apply或自然发布开始，不删除恢复审计、不回写旧URL、不整库回滚。保持timer暂停并核对真实ledger，再决定后续恢复。
