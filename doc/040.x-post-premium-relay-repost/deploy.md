# 部署与回滚

## 本轮状态

仅本地开发和离线测试，未部署生产。

## 部署前硬条件

1. 推送并锁定同一 GitHub commit；记录当前生产 release `0e03210...` 和下一次自然排期。
2. 等当前 schedule/manual/x_auto 发布均无 queued/running/unknown 后停 timer。
3. SQLite online backup；在副本执行 `ensure_storage`，验证 `quick_check=ok`、FK=0、旧 queue/log 摘要不变。
4. 记录历史 `needs_review + x_long_video_requires_premium` 恢复清单与审计行，确认均无当前集队列证据。
5. 同一 commit 构建不可变 release，先切 sidecar，再切 runner；自然调度验证，不为部署测试创建真实 Post。

## 部署后验证

- 健康接口、timer、原 material/drama/manual/x_auto 路径正常。
- SQLite `quick_check=ok`、FK=0；新 ledger 与队列一致。
- 会员列表只含 token 确认会员且公开账号，当前只有一个时负载全部落到该账号。
- 自然出现首条长视频时核对：原 Post 一次、目标 Repost 一次、剧集只推进一次。

## 回滚

若尚无任何 `premium_relay_repost` 队列，可停止 timer 并切回 `0e03210...` release。若已存在新队列/ledger，旧代码不是前向兼容回滚点：必须保持新 sidecar，先将所有中转任务收敛到 `reposted/failed/needs_review`，再仅关闭新增路由能力，禁止覆盖数据库备份或删除 ledger。
