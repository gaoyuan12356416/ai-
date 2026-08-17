# 部署文档

## 变更内容

普通 material schedule 增加稳定随机目标配对与非会员长视频 Premium relay Post -> Repost。

## 配置项

无新增环境变量。workflow version 固定为代码常量 `material-random-relay-v1`。

## 数据库变更

无新列/表。`ensure_storage` 幂等重建 relay insert/update triggers，使 material relay 仅允许 `schedule_run_id IS NOT NULL`。历史行不重写。

## 部署步骤

1. GitHub-first：提交/推送审核 commit，确认基线包含生产复合版本。
2. 暂停相关 X schedule/manual/auto timers，记录原状态；禁止 run-now。
3. 对在线 SQLite 使用 backup API，备份 release、unit/env、非 secret token hash/mode/owner；不得读取 Token 正文。
4. 生成不可变 release，先在备份副本运行两次 migration 与历史投影 diff。
5. 切换 Sidecar release，并同步 main API 的同一 `service.py`；仅重启受影响服务。
6. 恢复 timer 原状态，观察自然 `no_due/no_pending` 或自然计划，不创建真实 Post canary。

## 验证步骤

- exact release 运行专项与 focused server suite。
- `quick_check=ok`、foreign key=0，历史 queue/log/repost projection 不变。
- Sidecar/main API `service.py` hash 一致。
- 自然 timer 后核对 queue/log/pool/repost ledger；无 real X Post/Repost 作为部署证明。

## 回滚方案

1. 暂停相关 timers，切回上一不可变 release，恢复 main API 对应文件并窄重启。
2. 恢复 timers 原状态。
3. 默认保留当前 SQLite 与 Token 状态；新增 trigger 向后兼容，禁止用旧 SQLite/Token 覆盖生产新事实。
4. 若存在已冻结 material relay queue，回滚前先审计；不得删除 queue/ledger 或盲重发 unknown source/repost。

## 注意事项

- 严禁真实 X Post/Repost、run-now、manual publish 验证。

## 2026-08-17 实际发布记录

- GitHub branch：`codex/x-material-random-relay-20260817`；exact commit：`3c067dbe3a5b18ef5c34adb3ff373408604bca56`。
- 不可变 release：`/mnt/data-disk/x-post-automation/releases/3c067dbe3a5b18ef5c34adb3ff373408604bca56`。
- 上一 release：`/mnt/data-disk/x-post-automation/releases/e96ecad3b2af33cc6a53e3154f69e4a36dfff769`。
- 完整回滚包：`/mnt/data-disk/x-post-automation/backups/20260817T161912+0800-x-material-random-relay-3c067db`。
- 备份 SQLite 副本连续执行两次 `ensure_storage` 后 SHA-256 保持 `beb9340b7d5655652a6bd22ce9ff34dde065ad754b8fc871b34228a746306033`；24 tables、`quick_check=ok`、foreign key=0，manifest 全部通过。
- exact release `py_compile` 通过；服务器核心 focused suite 182/182 通过。包含 `test_x_post_schedule_runner` 的 Linux fixture 组合有 14 条固定 data-disk work-directory guard 拒绝，实际 `/mnt/data-disk/x-post-automation/daily-work` 已验证为非 symlink、权限正确、服务账号可读写，因此未放宽生产 guard。
- 暂停五个 timer 且确认所有 oneshot inactive 后，原子切换 `current`；把同一 Git commit 的 `features/x_posts/service.py` 同步到 main API，仅重启 `x-post-automation.service` 与 `drama-material-api.service`，再恢复五个 timer。
- Sidecar health 200，main API 匿名 material-pool 401；Sidecar/main `service.py` SHA-256 均为 `d00c773eb57e46d13196e2714edac443e279652316375677256822dbee051a61`。
- 切换后 SQLite `quick_check=ok`、foreign key=0；Token hash/mode/owner manifest 未变。基线 queue/published/active=`401/400/0`，publish-log/unknown/active=`401/0/0`，repost total/published/active=`27/27/0`，pool published/unpublished=`156/204`，切换前后完全一致。
- 16:24 X Auto 首周期与 sidecar 同秒启动，出现一次 `x_auto_post_sidecar_unavailable`；16:25 起自然 scheduler/runner 周期连续成功，属于启动竞态且已自行恢复，没有手工 run-now。
- 16:59 的既有 material 自然槽创建 run `217`（expected=5）。截至 17:23，它仍在全批预检且 queue=0；同一 CPU PID 持共享锁并连接健康的 GPU repair tunnel。GPU 已完成第二个视频的转码（source `114,035,606` bytes，repaired `455,929,676` bytes），正在通过四条 ESTAB 连接上传 COS，因此 GPU 利用率回落到 0% 不代表卡死。CPU 的单次 repair timeout 为既有 `3600` 秒；未超时、未中断、未人工重试。
- 该自然任务作为非阻塞运行观察留在后台。17:23 不变量仍为 queue/published/active=`401/400/0`、publish-log/published/unknown=`401/400/0`、run 217 queue=0、pool=`156/204`；因此尚无本次自然槽 X Post/Repost 写入。后续由现有超时/unknown fence 自然收敛，不以等待大文件上传作为代码交付门禁。

## 实际回滚命令边界

1. 停止 `x-post-schedule.timer`、`x-post-schedule-claim.timer`、`x-post-manual.timer`、`x-auto-post-scheduler.timer`、`x-auto-post-runner.timer`，等待关联 oneshot inactive。
2. 原子切回 `/mnt/data-disk/x-post-automation/releases/e96ecad3b2af33cc6a53e3154f69e4a36dfff769`。
3. 从回滚包仅恢复 `payload/main-service.py` 到 `/root/drama_material_service/features/x_posts/service.py`，重启 `x-post-automation.service` 与 `drama-material-api.service`。
4. 恢复原 timer 状态。保留当前 SQLite 和 Token；禁止用备份覆盖部署后可能新增的自然 Post/Repost/Token 轮换事实。
