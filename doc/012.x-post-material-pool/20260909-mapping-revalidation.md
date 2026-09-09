# X 素材映射错误修复验收

- 生产代码提交：5650434e2d5d0c62f9663587b6d32c5d7ae1220f，已推送 GitHub 并由服务器拉取核对。
- 服务器：43.166.187.96。
- X 发布版本：/mnt/data-disk/x-post-automation/releases/5650434e2d5d0c62f9663587b6d32c5d7ae1220f。
- 后台同步文件：/root/drama_material_service/features/x_posts/service.py。
- 回滚备份：/mnt/data-disk/x-post-automation/backups/mapping-revalidate-20260909-5650434。
- 仅修改业务文件 features/x_posts/service.py；新发布目录保留原线上其他业务代码。X sidecar 和主 API 的业务文件一致。

## 修复与完整预检

将历史 drama_mapping_ambiguous 纳入原有候选重查流程。每次重新验证当前源数据，真实歧义继续拦截；候选枚举不清空错误。正常队列只在完整预检通过并冻结队列时事务性解除旧错误。已绑定素材、未知发布结果和去重规则保持有效。

冻结 40 条未绑定素材，使用现有 x_post_media_repair_backfill.py 执行完整源数据/媒体预检。结果 38 条恢复可用，其中 9 条 repaired_ready，另 29 条 validated_ready。40 条全部写回准确校验结果。

剩余两条：5978307、6120090，均为 invalid_media_type：下载文件类型与源素材类型不一致，需要修正源库媒体类型或提供对应的正确文件。本次未绕过类型一致性检查。

先前严格视频元数据检查的 36/40 不等于最终线上预检：现行正式入口允许图片及保留有效视频链接的删除记录。最终验收以本次完整流程的 38/40 为准。

## 验证

- 本地和生产副本各 294 项池、调度、队列、媒体预检与来源选择测试通过。
- X health HTTP 200；主 API 管理素材接口返回预期 Cookie 门禁 HTTP 401；两个服务 active，NRestarts=0。
- 修复前后 queue=1760，publish_log=1760，published=1729，unknown=0 完全一致。没有创建、重放 Post/Repost 或修改历史发布台账。
- 原有 5 个活动定时器全部恢复；原先 inactive 的 x-auto-post-runner.path 保持 inactive。
- 核查时今天素材池剩余冻结时点为北京时间 21:32。未来实际发布仍执行账号资格、同语言、媒体和去重校验；未进行真实发帖验证。
- 未修改 ROAS、语言、账号和短剧池配置；英文素材/短剧供给不足仍须按运营需求补充。
- X 发布运维技能已更新历史映射错误的重查规则。

## 精确回滚流程

1. 暂停本备份 timers.json 中列出的触发器，等待已有 worker 正常结束，先核对未知发布结果；不得强行中断已发出的 X 写请求。
2. 将 /opt/x-post-automation/current 原子指回 /mnt/data-disk/x-post-automation/releases/2d213b8f9837f49d0fa566da83e27a558148dd0e。
3. 将本备份 api-service.before.py 恢复到 /root/drama_material_service/features/x_posts/service.py。
4. 执行 systemctl restart x-post-automation.service drama-material-api.service，重新核对 health、Cookie 门禁和两服务 active。
5. 按 timers.json 只恢复原来 active 的触发器。
6. 不覆盖恢复旧 SQLite。媒体校验结果有 pool-before.json / pool-after.json；若另行授权回退校验结果，必须核对当前仍未绑定且状态等于 after 快照，再逐行条件更新，保留所有新增发布历史。
