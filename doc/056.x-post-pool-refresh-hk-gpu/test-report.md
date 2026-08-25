# 测试报告

## 测试结论

通过。代码、GitHub-first 部署、香港 GPU 迁移、素材状态刷新、媒体重制、精确删除、
timer 恢复和零真实发布边界全部满足验收标准。

## 测试范围

- selector 标签/违规兼容、`jp`→`ja`、语言容量、Premium relay。
- 显式回填 Premium 时长策略、`--force-repair`、NVENC、COS 上传/HEAD、CPU 二次探测。
- CPU/香港/旧 GPU 服务拓扑、数据库状态、deferred、删除门禁、timer 自然 tick。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 聚焦离线回归 | 167 | 167 | 0 | 0 |
| X 全量离线回归 | 506 | 505 | 0 | 0（1 条环境条件跳过） |
| CPU 精确 release 回归 | 32 | 32 | 0 | 0 |
| 香港 GPU 精确 release 回归 | 20 | 20 | 0 | 0 |
| 生产媒体强制重制 | 8 | 8 | 0 | 0 |
| 历史状态刷新 | 212 | 212 | 0 | 0 |
| 精确删除 | 3 | 3 | 0 | 0 |

## 缺陷情况

- BUG-001：已有语言账号但本批容量满仍写 `material_language_not_scheduled`；已修复。
- BUG-002：显式 backfill 固定虚拟账号缺少 Premium 能力，8 条在 GPU 前误报会员错误；
  已增加 Premium 校验与显式强制重制，生产 8/8 通过。
- timer 恢复首 tick 因 manual 相位与 schedule `:10` 重合而 `skipped_locked`；相位调整后
  下一分钟自然结果为 claim 0 / schedule `no_due` / manual `no_pending`。

## 验证证据

- GitHub/生产提交：`fba8ff603e979b443339108cb2ce45c975fbd39f`。
- CPU 备份：`/mnt/data-disk/x-post-automation/backups/20260825T175100+0800-force-backfill-pre-fba8ff6-complete`。
- 香港备份：`/root/backups/20260825T175010+0800-x-post-force-repair-pre-fba8ff6`。
- 回填报告：`media-backfill-hk-force-canary.json`、`media-backfill-hk-force-remaining7.json`；
  分别 1/1 与 7/7 `repaired_ready`，合计 8 次实际修复。
- 删除审计：`delete-other-errors-before.json`、`delete-other-errors-result.json`；仅删除 86/296/297。
- 最终数据库：素材池/队列/日志/unknown=`841/627/627/0`；指定六码 0；仅剩 5 条 deferred；
  integrity `ok`、foreign-key violations 0。
- CPU Sidecar、香港 worker/隧道 active，NRestarts 0；旧 GPU worker active、旧隧道 inactive。

## 遗留风险

- 5 条 deferred 仍需后续自然素材排期读取当前权威 `deploy_time` 后再选择；当前没有队列绑定，
  不会提前发布，也不会永久排除。
- 未使用真实 X Post/Repost 验收；最终外部发布仍受实时 Token、会员资格、X 上游与 rate limit 约束。

## 发布建议

维持当前版本。日常运营遇到错误时按 `doc/055.x-post-deferred-deliverable/error-catalog.md`
的中文含义与处理建议处置；未知写结果仍禁止自动重试。
