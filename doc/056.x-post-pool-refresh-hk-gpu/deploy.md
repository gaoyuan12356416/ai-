# 部署文档

## 变更内容

CPU 发布 selector/schedule 修复；香港 GPU 新增 X media repair worker 与反向隧道；旧 GPU 保留 worker 并停用旧隧道；执行定向回填、状态刷新和精确删除。

## 配置项

- 香港 worker：`/etc/x-post-media-repair.env`、`/etc/x-post-media-repair.token`、`/etc/x-post-media-repair.cos.env`，均为 root 只读。
- 香港工作目录：`/var/lib/x-post-media-repair`。
- 香港 Python：`/opt/x-post-media-repair/venv`，依赖由冻结 requirements 安装。
- 隧道密钥与 known_hosts：`/etc/x-post-media-repair-tunnel`，CPU 只授权监听 `127.0.0.1:18820`。

## 数据库变更

无 schema 变更。写入前使用 SQLite online backup；刷新和删除均在 `BEGIN IMMEDIATE` 中先复核冻结指纹、错误码、未发布、无队列和无活动占用。

## 部署步骤

1. 推送 GitHub 提交并记录 commit；三端备份现有 release/unit/env 哈希与 SQLite。
2. 香港 GPU 安装 Python 3.9，创建 venv 并安装冻结依赖；部署同一 GitHub commit。
3. 安装独立 COS/env/token、worker unit 和专用隧道密钥；仅启动 worker。
4. 完成本机 health、鉴权拒绝、NVENC 样例和 COS 配置启动校验。
5. 停旧 GPU tunnel，启香港 tunnel；验证 CPU 18820 的 sshd 对端与 health。失败立即恢复旧 tunnel。
6. CPU 发布 GitHub commit，重启 sidecar，执行聚焦 health/测试。
7. 对 8 个修复/COS 素材运行带 `--force-repair` 的显式 backfill；刷新四类历史状态；删除冻结 3 条。
8. 核对错误分布、deferred、队列/日志、timer、systemd、SQLite integrity。

## 验证步骤

- `systemctl is-active x-post-media-repair.service x-post-media-repair-tunnel.service`
- `curl http://127.0.0.1:8820/health`（香港）与 `curl http://127.0.0.1:18820/health`（CPU）
- `nvidia-smi`、NVENC 样例 `ffmpeg` + `ffprobe`
- `python scripts/x_post_media_repair_backfill.py --force-repair --material-id ... --report-path ...`
- SQLite 精确数量、指纹、`PRAGMA integrity_check`、队列/日志前后差异为 0。

## 回滚方案

- CPU：将 `/opt/x-post-automation/current` 指回 `960816e64e9d889d99fad313466a655316692ed6` 并重启 sidecar。
- GPU：停香港 tunnel，启旧 GPU `x-post-media-repair-tunnel.service`，确认 CPU 18820 恢复。
- 状态数据：从本次 online backup 恢复，或按保存的逐条 before-image 逆向更新；被删 3 条可从 backup 恢复。
- 香港 worker 可保持停止状态，不影响旧 GPU 回滚。

## 注意事项

不调用任何 X 发布接口；切换期间暂停 X schedule/claim timer，完成后恢复原状态；日志、命令和报告不得输出 Token/COS 密钥。

## 生产执行结果（2026-08-25）

- GitHub/CPU/香港 GPU 统一代码提交：`fba8ff603e979b443339108cb2ce45c975fbd39f`。
- CPU release：`/mnt/data-disk/x-post-automation/releases/fba8ff603e979b443339108cb2ce45c975fbd39f`；
  前一 release 为 `9c032348f06b19834b72dc6330258a01e1b4be95`。
- 香港 release：`/opt/x-post-media-repair/releases/fba8ff603e979b443339108cb2ce45c975fbd39f`；
  旧 GPU worker 保持 active，旧隧道保持 inactive，作为回滚冷备。
- CPU 完整备份：`/mnt/data-disk/x-post-automation/backups/20260825T175100+0800-force-backfill-pre-fba8ff6-complete`；
  香港备份：`/root/backups/20260825T175010+0800-x-post-force-repair-pre-fba8ff6`。
- canary 1/1、剩余批次 7/7 均为 `repaired_ready`，共 8 次实际 GPU 重制；报告 SHA-256
  分别为 `19c0dc8c...aab8f` 与 `433a1715...fc82`。
- 四类历史状态按冻结清单清空 212 条；池 86/296/297 在无队列、无活动占用门禁下删除 3 条；
  5 条 `drama_not_yet_deliverable` 保持 deferred。
- 最终素材池/队列/日志/未知结果为 `841/627/627/0`；指定六码均为 0，SQLite integrity `ok`、
  foreign-key violations 0。
- schedule/claim/manual timers 恢复 active+enabled；daily timer 保持原 masked。自然观察为
  `claimed_or_pending_count=0`、`no_due`、`no_pending`，没有用真实 Post/Repost 做验收。

## 精确回滚

1. 停 `x-post-schedule.timer`、`x-post-schedule-claim.timer`、`x-post-manual.timer`，等待在途 oneshot 结束。
2. CPU 将 `/opt/x-post-automation/current` 原子指回
   `/mnt/data-disk/x-post-automation/releases/9c032348f06b19834b72dc6330258a01e1b4be95`，重启 `x-post-automation.service`。
3. GPU 停香港隧道，启动旧 GPU `x-post-media-repair-tunnel.service`，确认 CPU 18820 v5 health；
   如需回退香港 worker，再将其 current 指回 9c release。
4. 默认保留当前 SQLite/Token；仅当确认需要撤销本次池状态时，按备份内 before-image 做精确逆向，
   不用整库覆盖后来产生的队列、日志或 Token 事实。
