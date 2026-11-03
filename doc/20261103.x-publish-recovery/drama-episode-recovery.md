# 短剧 1029 / 1031 第二次精确恢复

本文件记录本地实现与操作边界；不代表已在线执行恢复。

两行原时段、35 个账号及配置 27 保持不变。第一次恢复审计 `x_post_schedule_zero_write_resume_audit` 保留，继续作为 1029 跨 cutover 的原身份凭证。第二次独立审计表 `x_post_schedule_episode_filter_retry_audit` 只能追加，保存完整原行、原 `plan_attempted_at`、首轮完整审计及已部署修复证据。

| run | 原日期 / 时段 | 本次计划尝试 UTC | 失败完成 UTC |
| --- | --- | --- | --- |
| 1029 | 2026-11-02 / 16:28 | 2026-11-03T10:53:01Z | 2026-11-03T10:53:01Z |
| 1031 | 2026-11-03 / 08:44 | 2026-11-03T10:52:38Z | 2026-11-03T10:52:39Z |

只接受以上两行的 `failed_preflight`、精确错误 `x_post_drama_episode_already_used` / `短剧集数已被其他发布队列占用`，且无 started、queue、log 或任何成功/失败/未知计数。原 snapshot 必须为空。完整原行、首轮审计及账号顺序均在 apply 的 `BEGIN IMMEDIATE` 中再次比对。

服务端的这条精确错误来自同一 SQLite 事务内的 episode key 守卫，明确 `ROLLBACK`，位于任何 queue INSERT 之前；该路径没有短链写入或 X Post 调用。专项测试模拟旧 selector 泄漏并记录 SQL，证明 BEGIN / ROLLBACK 存在、无 INSERT / UPDATE / DELETE、无短链或 Post 调用。此证明不适用于其他错误或其他尝试时间。

## 部署和执行

先通过 GitHub 推送并部署包含 episode 过滤修复的提交，再生成计划。`DEPLOYED_SHA` 必须为该实际部署的完整 40 位 SHA；`REPOSITORY` 为已拉取此提交的本地 Git 仓库，`SERVICE_PATH` 为运行服务加载的 service.py。工具验证提交包含两处 episode 过滤、部署文件与 Git blob 逐字节一致，并保存 SHA256、路径及 symlink 解析路径。生成计划、apply 前和持锁事务内均核对部署证据。

```sh
python3 scripts/recover_x_drama_episode_selection.py plan \
  --db /var/lib/x-post-automation/accounts.sqlite3 --run-ids 1029,1031 \
  --deployed-commit "$DEPLOYED_SHA" --repository "$REPOSITORY" \
  --service-path "$SERVICE_PATH" --output "$PRIVATE_DIR/episode-plan.json"

python3 scripts/recover_x_drama_episode_selection.py apply \
  --db /var/lib/x-post-automation/accounts.sqlite3 \
  --plan "$PRIVATE_DIR/episode-plan.json" --plan-sha256 "$PLAN_SHA256" \
  --backup-dir "$PRIVATE_DIR/backups" --actor "$ACTOR" \
  --reason '恢复精确零写入 episode selector 拒绝；保留所有历史集数占用' \
  --output "$PRIVATE_DIR/episode-apply.json"
```

apply 先在线备份、quick_check、计算备份哈希，然后在单个事务内追加两条审计并将两行改为 claimed、仅清除已审计的本次 attempt/error/finished。冻结日期、账号、素材快照、模板、策略、原首轮审计均不改；不创建发布队列，不调用平台。后续 runner 必须正常预检。中断会整体回滚；相同 plan 重复 apply 返回已应用，不重置任何后续新尝试。

候选过滤继续保留历史 cancelled / failed / published 等所有队列的 episode key 占用。被占用的原账号仍绑定原剧目，不替换、不推进下一集、不生成新重播代次。手动 1389 的账号 43 另有 q5524 未知结果，本脚本完全不涉及该任务。

## 本地验证

恢复专项覆盖原审计和冻结身份不变、旧时段准入、追加审计不可变、后续 attempt 幂等、部署漂移、未知计数、失败时间漂移、残留队列、批量中断回滚、首轮审计及账号范围漂移。服务测试另覆盖所有队列状态的占用过滤、未绑定历史队列、健康子集正常建队和精确拒绝事务边界。

`python -m unittest scripts.test_x_post_multi_schedule_store scripts.test_x_drama_episode_selection_recovery`：165 项通过。相关文件 `py_compile` 和 `git diff --check` 通过。
