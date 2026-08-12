# 部署与回滚

## 部署前

1. 记录当前 CPU/GPU release commit、服务状态和 timer 状态。
2. 备份当前 release、环境示例对应的实际 profile 配置及 SQLite；不读取或输出 Token 内容。
3. 确认无自然发布正在执行，不在活跃上传窗口切换。

## 部署

1. 从 GitHub 拉取精确 commit。
2. CPU 和 GPU 同步部署 `duration-policy-v4` 代码。
3. 将 `X_POST_DAILY_REPAIR_PROFILE` 更新为 `x-h264-nvenc-720-duration-policy-v4`。
4. 先检查 CPU/GPU health 的 profile 一致，再仅重启受影响服务。
5. 不创建人工真实 Post；通过自然调度或无到期窗口验证。

## 验证

- 服务 active、health 正常，CPU/GPU profile 均为 v4。
- 自然调度无 `profile_mismatch`、无未知写入、无重复 queue/log。
- 会员资格刷新失败或降级时仍在 X 写入前拒绝。

## 回滚

停止调度后切回部署前 commit 与 v3 profile，CPU/GPU必须同时回滚；保留数据库、队列、日志和 Token，不删除任何历史发布证据。若已存在 v4 修复产物，不得让 v3代码自动重用，应保持任务终态或人工核对后再恢复调度。

## 2026-08-12 生产部署记录

- 精确提交：`46e0720b8eb6b3c7b29cb92830f3c74cec3dbe70`。
- CPU/GPU 已同步切换，`X_POST_DAILY_REPAIR_PROFILE=x-h264-nvenc-720-duration-policy-v4`，CPU 反向 health 返回 v4。
- CPU 旧 release：`49b42bbeb5902fd7662732bafa333c89bb8dcf8d`；GPU 旧 release：`29bd90034396c597b30ceb7135376efb750ec886`。
- 仅重启 `drama-material-api.service`、`x-post-automation.service`、`x-auto-post-service.service`、GPU repair/tunnel 及相关 X timer；未触碰其他业务服务。
- 并发开发的 X 自动模板资产逐文件比对一致，三项上线 gate 继续关闭。
- 下一自然计划为素材池 15:42、短剧池 16:08；部署验收没有主动触发真实发布。
