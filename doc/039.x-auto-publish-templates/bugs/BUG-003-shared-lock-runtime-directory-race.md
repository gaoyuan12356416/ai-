# BUG-003 共享发布锁目录被 oneshot 删除

## 发现阶段

2026-08-12 生产只读日志与 systemd 生命周期审计。

## 现象

X Auto scheduler 在 05:21、08:49、09:19 三次偶发退出 1；`/run/x-auto-post` 与 `/run/x-post-daily` 会在相关 oneshot 结束后消失。

## 复现步骤

让多个声明相同 `RuntimeDirectory` 的 persistent/oneshot unit 交错启动和停止，观察目录及锁文件 inode。

## 期望结果

共享 flock 目录由单一持久机制管理，任何一个 unit 停止都不能删除目录或替换锁 inode。

## 实际结果

systemd 239 会在 transient owner 停止时清理共享目录；另一个进程可能在 `mkdir` 与 `open(lock)` 间失败，甚至存在同路径不同 inode 的串行化绕过风险。

## 根因分析

X Auto 与既有 X 多个 unit 同时把共享路径声明为各自 `RuntimeDirectory`，目录生命周期没有唯一 owner。

## 修复说明

- 新增 `deploy/x-post-runtime-tmpfiles.conf`，以 `0700 x-post-daily:x-post-daily` 持久创建两个共享目录。
- 从 X Auto 四个 service 与既有 X daily/manual/schedule/catchup unit 移除 `RuntimeDirectory=`。
- 增加 tmpfiles 启动顺序和目录存在条件，缺目录时 fail-close。
- runner path 同步增加目录条件；新增唯一 owner 和 Linux flock/inode 回归。

## 影响文件

- `deploy/x-post-runtime-tmpfiles.conf`
- `deploy/x-auto-post-*.service`
- `deploy/x-auto-post-runner.path`
- `deploy/x-post-{daily,manual,schedule,catchup}.service`
- `scripts/test_x_auto_post_deploy.py`

## 验证命令与结果

- 相关组合回归：113/113 通过。
- 生产 Linux 临时目录动态测试：peer lock 被拒绝，目录存活且 inode 不变。
- `git diff --check` 通过。

## 回归结论

已部署。10:31–10:45 的 X auto scheduler/runner 与既有 schedule/manual 多轮自然 oneshot 全部成功，两个目录 inode 始终为 `41385288/41385290`。三 gate 继续全关且 X Auto 表全空。
