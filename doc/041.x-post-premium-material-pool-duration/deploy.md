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
