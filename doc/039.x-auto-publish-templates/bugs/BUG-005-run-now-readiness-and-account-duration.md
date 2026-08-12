# BUG-005：手动执行被生产门禁拦截及标准账号误选长视频

## 现象

- 2026-08-12 17:33:03、17:33:41，模板 `1` 的两次
  `POST /api/admin/x-auto-publish/templates/1/run-now` 均返回 HTTP 409：
  `x_auto_live_gates_closed`。
- 两次失败都发生在创建运行之前；X Auto 的 run/task/ledger 均为 0，
  既有 X queue/log/Post 也没有因该请求增加。
- 只读预览随后为账号 `1` 选择了 270 秒素材，但该账号当前 token
  `subscription_type=none`，最终媒体预检会按 140 秒标准账号上限拒绝。

## 根因

1. 首次安全部署时三道生产门禁保持为 0，运营开始手动测试前没有完成
   账号、指标、短链检查并显式开启。
2. 模板配置允许 1–600 秒，但预览和第一阶段选材只使用模板上限，
   没有把任务所绑定账号的当前 token 会员资格折算为有效选材上限。

## 修复

- 保留原三道生产门禁，不增加旁路：真实手动执行和自动排期仍必须同时满足
  `LIVE_ENABLED + ACCOUNT_AUDIT_APPROVED + URL_PROPERTY_VERIFIED`。
- 生产开启门禁前必须确认：模板范围、账号 `active + approved + publish_eligible`、
  token 权限/文件权限、完整指标窗口和真实短链 200/no-store。
- 预览使用刚验证的账号快照；实际运行使用创建任务时冻结的账号快照。
  标准账号的有效最大时长为 140 秒；只有 token 同时报告
  `long_video_eligible=true` 且会员为 `basic/premium/premium_plus`，才保留模板
  最多 600 秒的范围。最终准备阶段仍重新验证账号并执行原媒体门禁。
- 当模板最小时长已经高于账号有效上限时，返回 `no_candidate`，不得选择后
  再以媒体错误失败。
- 门禁关闭错误改为中文，但稳定错误码保持 `x_auto_live_gates_closed`。
- 账号展示统一为会员账号只显示 Basic/Premium/Premium+；无会员或资格未知账号才显示
  “最长 140 秒”。展示变化不参与后台资格判断。

## 回归与发布边界

- 离线 X Auto 聚焦回归必须覆盖标准账号、会员账号、伪造会员布尔值、
  空时长交集、预览和实际选择调用点。
- 部署切换 X Auto 不可变 release、同步两份静态根目录、备份并更新
  `/etc/x-auto-post.env`，重启 `x-auto-post-service.service`；不重启现有 X Sidecar、
  主 API 或 Nginx。
- 部署验收不得替用户点击“手动执行”，不得额外创建真实 X Post。
- 代码回滚切回旧 release；配置回滚把三道门禁恢复为 0。存在任何已创建的
  run/queue/Post 时均保留当前 SQLite 账本，不得恢复旧数据库覆盖发布事实。
