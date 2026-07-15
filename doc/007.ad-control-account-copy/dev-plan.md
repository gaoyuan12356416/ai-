# 开发计划

## 开发范围

本次交付实现 FB Campaign 规则组升级、Campaign 观察/试算、复制安全编排契约、账号级 UI 和完整测试；Campaign 正式复制、复制结果 ads_ai 写入及全部 Ad 候选/执行能力后置。Ad 本期仅可保存配置。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| C0 基线与本地/线上备份 | Codex | Git bundle、线上 C0 目录 | 已完成 |
| 需求、SA、接口和部署契约 | Codex | `doc/007.ad-control-account-copy` | 已完成（含生产证据） |
| 复制结果 ads_ai schema/事务写入 | 后续需求 | 用户后续明确授权并指定写法 | 本期取消（DDL 环境已修复，仍不在本轮执行） |
| 规则模型、旧聚合迁移和 Campaign 观察链路 | created_data_write_patterns | `app.py`、runner、copy engine | 已完成（本地） |
| 规则组页面升级、`partial_enabled` 与 legacy action 兼容 | rule_dimension_review | `static/ad-control-*` | 已完成（本地） |
| 集成、回归、代码评审 | Codex | 全部变更 | 已完成（最终 fresh-cache 174/174） |
| 生产 overlay 合并与生产 Python 环境完整验证 | Codex | 线上共享 monolith 副本/发布包 | 已完成 |
| GitHub-first 暗发布 | Codex | 精确 commit/发布包/线上检查点 | 已完成 |
| Campaign 观察验收 | Codex + 业务方 | 已批准测试账号 | 待批准 |

## 编译 / 构建命令

```powershell
python -m py_compile app.py scripts/ad_control_rule_runner.py features/ad_control_copy_engine/service.py features/ad_control_execution_log/service.py deploy/apply_ad_control_execution_log_fix.py deploy/apply_ad_control_account_copy_v2.py deploy/migrate_ad_control_account_copy_v2_sqlite.py
python -m unittest discover -s tests -p "test_ad_control*.py" -v
node --check static/ad-control-pages.js
python -m unittest tests.test_ad_control_account_copy_deploy -v
```

## 风险与依赖

- 所有 Meta 编排单测必须使用 fake/stub；本期不得新增 copied created_data/lineage/intent MySQL DDL/DML，测试不得连接生产写节点。既有 `ads_ai.ad_control_action_log` 审计不属于复制结果写入。
- `AD_CONTROL_COPY_ENABLED` 默认关闭，代码上线不等于开放复制。
- 本期不进行 copied created_data/lineage/intent DDL/DML；正式 copy 必须在 Meta POST 前返回 `copy_persistence_not_configured`。既有 `ad_control_action_log` 审计链路保持原样。
- DDL 环境问题已修复只作为后续实施条件；未获得用户下一次明确授权前，不建、不写 copied created_data/lineage/intent。`ads_ai.ad_control_action_log` 是现有审计表，不计入复制结果落表。
- 线上新版 action-log 的 writer/reader 分离、固定库表、超时/并发上限及 runner 更新不立即 upsert 重试必须保持。每次发布前都必须将当前线上副本作为 current-live fixture 执行补丁 check/apply/二次幂等和契约断言，不通过则不得发布。
- 生产 `app.py` 不得由 checkout 整文件盲目覆盖。实际发布使用 `deploy/apply_ad_control_account_copy_v2.py`：只接受已核实 source commit 的精确 blob，在临时目录应用 source→target Git diff 并核对 target blob，再生成持久化唯一字节备份和原子替换。所有受控 `app.py` 发布必须使用统一 `/var/lock/drama-material-service.deploy.lock`；本次窗口禁止其他发布/热修。锁外人工覆盖不受 advisory lock 约束，必须靠排他窗口和写前/写后 hash 阻断。
- API/runner 启动会触发 schema ensure，因此真实 SQLite 的 ensure 与三条已核实 group ID 的精确 owner 迁移必须在 API/runner 均停止后、重启前完成；失败即恢复 C1 SQLite，不允许带 `owner_user_id='codex'` 的中间态启动服务。
- PAUSED/ACTIVE Canary 延后到用户确认落表方式后的下一需求。
- 当前 stale-preview 安全门禁在单个 Campaign pause 期间持有全局 `JOB_DB_LOCK` 跨 Graph GET/POST；最坏约 60 秒的同进程 SQLite 写阻塞作为 P2 运行风险进入暗发布监控，不得在本地测试通过后宣称生产无影响。

## 完成记录

- 2026-07-15：从 commit `352bfb4e96abe6bf50b76cacb3f25e4608774c92` 创建独立工作树 `D:\codex\ai-drama-material-service-ad-control-copy` 和分支 `codex/ad-control-copy-rules`。
- 2026-07-15：C0 本地 bundle 与线上全量配置/SQLite 备份已完成并校验。
- 2026-07-15：发现 16:01 已并发上线 daily-log overlay，重新以生产组合提交 `0a4c408eb7d027eb60eb15496c6dae48443a2a1c` 为合并基线；V2、daily/raw 日志读模型和统一缓存版本合并，并收口 mixed 批次、exact deploy、owner 迁移以及产品账号列表 owner/cache 回归后 fresh-cache 171/171 通过。exact-source Git diff 部署器 12/12、SQLite owner 迁移器 8/8（含真实 target app ensure/owner 可见性集成）通过；生产 staging、C1 与线上 smoke 仍待完成。
- 2026-07-15：正式发布前发现 18:22 playable preview 并发发布，旧基线和首次检查点作废。恢复服务后将 `8c559a78475a7972542746f1f8de1fcab4e7be3f` 合入 V2，形成原 V2 运行提交 `b3c3e6a2d6556d7dad4c79082a324235ad0f8379`；从新 live 基线重建 staging、重跑该阶段171/171与playable回归并建立原V2 C2。
- 2026-07-15：生产 exact-source overlay、owner 迁移（check 零写、apply 3、幂等 0）及 API/worker/Nginx/浏览器 smoke 完成。首次恢复 cron 后的 18:50 自然 tick 安全阻断为 `live_preview_blocked`：requested/success=0/0、Meta 写入为 0；定位为 BUG-007（空 Campaign 白名单误报且无到期账号仍写 action 审计），立即在下一 tick 前仅暂停 ad-control cron。
- 2026-07-15：以 `7f65cf9bf6799fb0a086238d41f569c2b206e820` 修复空白名单，以 `4527303100a38db26f0f2ac0825ed6616c16247a` 增加 `scheduled_due_count` 和 `no_accounts_due` 零 action 跳过；生产 staging fresh-cache 174/174，建立 C3 `/root/backups/drama_material_service/20260715T111700Z-ad-control-v2-hotfix-c3-4527303` 并完成 exact overlay。19:25 自然 tick 返回 `skipped/no_accounts_due`，requested/success/error=0/0/0，SQLite action 保持 17，原 cron 最终保持启用。复制持久化和真实 Meta/Ad 执行仍按范围后置。
