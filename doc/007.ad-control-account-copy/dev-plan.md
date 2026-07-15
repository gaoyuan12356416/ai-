# 开发计划

## 开发范围

本次交付实现 FB Campaign 规则组升级、Campaign 观察/试算、复制安全编排契约、账号级 UI 和完整测试；Campaign 正式复制、复制结果 ads_ai 写入及全部 Ad 候选/执行能力后置。Ad 本期仅可保存配置。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| C0 基线与本地/线上备份 | Codex | Git bundle、线上 C0 目录 | 已完成 |
| 需求、SA、接口和部署契约 | Codex | `doc/007.ad-control-account-copy` | 已完成（待部署后回填线上证据） |
| 复制结果 ads_ai schema/事务写入 | 后续需求 | 用户后续明确授权并指定写法 | 本期取消（DDL 环境已修复，仍不在本轮执行） |
| 规则模型、旧聚合迁移和 Campaign 观察链路 | created_data_write_patterns | `app.py`、runner、copy engine | 已完成（本地） |
| 规则组页面升级、`partial_enabled` 与 legacy action 兼容 | rule_dimension_review | `static/ad-control-*` | 已完成（本地） |
| 集成、回归、代码评审 | Codex | 全部变更 | 已完成（fresh-cache 90/90） |
| 生产 overlay 合并与生产 Python 环境完整验证 | Codex | 线上共享 monolith 副本/发布包 | 未完成 |
| GitHub-first 暗发布 | Codex | 精确 commit/发布包/线上检查点 | 未完成 |
| Campaign 观察验收 | Codex + 业务方 | 已批准测试账号 | 待批准 |

## 编译 / 构建命令

```powershell
python -m py_compile app.py scripts/ad_control_rule_runner.py
python -m unittest discover -s tests -p "test_ad_control*.py" -v
node --check static/ad-control-pages.js
```

## 风险与依赖

- 所有 Meta 编排单测必须使用 fake/stub；本期不得新增 copied created_data/lineage/intent MySQL DDL/DML，测试不得连接生产写节点。既有 `ads_ai.ad_control_action_log` 审计不属于复制结果写入。
- `AD_CONTROL_COPY_ENABLED` 默认关闭，代码上线不等于开放复制。
- 本期不进行 copied created_data/lineage/intent DDL/DML；正式 copy 必须在 Meta POST 前返回 `copy_persistence_not_configured`。既有 `ad_control_action_log` 审计链路保持原样。
- DDL 环境问题已修复只作为后续实施条件；未获得用户下一次明确授权前，不建、不写 copied created_data/lineage/intent。`ads_ai.ad_control_action_log` 是现有审计表，不计入复制结果落表。
- 线上新版 action-log 的 writer/reader 分离、固定库表、超时/并发上限及 runner 更新不立即 upsert 重试必须保持。每次发布前都必须将当前线上副本作为 current-live fixture 执行补丁 check/apply/二次幂等和契约断言，不通过则不得发布。
- PAUSED/ACTIVE Canary 延后到用户确认落表方式后的下一需求。
- 当前 stale-preview 安全门禁在单个 Campaign pause 期间持有全局 `JOB_DB_LOCK` 跨 Graph GET/POST；最坏约 60 秒的同进程 SQLite 写阻塞作为 P2 运行风险进入暗发布监控，不得在本地测试通过后宣称生产无影响。

## 完成记录

- 2026-07-15：从 commit `352bfb4e96abe6bf50b76cacb3f25e4608774c92` 创建独立工作树 `D:\codex\ai-drama-material-service-ad-control-copy` 和分支 `codex/ad-control-copy-rules`。
- 2026-07-15：C0 本地 bundle 与线上全量配置/SQLite 备份已完成并校验。
- 2026-07-15：本地 fresh-cache ad-control 全量回归 90/90 通过；真实补丁 apply 后在临时 app 上再跑 90/90 通过，首次备份 hash 与源 app 匹配、二次应用 unchanged。current-live fixture 验证还确认 writer/reader 端口、3/5 秒超时、`AD_CONTROL_LIVE_MAX_WORKERS=4` 及 7 个线上安全函数 hash 均不被补丁改写；生产 overlay 完整验证、GitHub exact-commit 发布与暗发布均尚未完成，不构成上线证明。
