# 部署文档

## 变更内容

将 FB Page Token 资格从 `status=0` 改为 `status<>1`，同步组统计、Page
快照、名称和执行凭证查询。

## 配置项

无。保持当前 `FB_AUTO_PREBUILD_ENABLED` 与 `FB_AUTO_POST_LIVE_ENABLED` 值。

## 数据库变更

无 DDL/DML。只读 MySQL 谓词变化；SQLite 历史事实不回填。

## 部署步骤

1. 本地测试通过后提交并推送 `codex/fb-auto-non-banned-token-20260824`，记录精确 SHA。
2. 验证服务器 GitHub SSH 身份与精确提交可读取。
3. 在数据盘创建时间戳备份，记录旧 release 指针，备份旧
   `repositories.py`、unit/env 元数据，并对两个 SQLite 做 online backup、
   `quick_check` 与 SHA-256。
4. 停止 scheduler/plan/prepare/runner/reconcile timers；确认没有
   `running/preparing/submitted/unknown` 的危险切换状态。
5. 从 GitHub 精确 SHA 创建 `/opt/fb-auto-post/releases/<sha>`，先在新 release
   运行语法和 FB 专项测试。
6. 原子切换 `/opt/fb-auto-post/current`，只重启 `fb-auto-post-service.service`。
7. 验证 health/日志/只读 Page 池和 SQLite 后恢复五个运行型 timers；指标 timers
   保持原状态。

## 验证步骤

- 新 release `repositories.py` SHA 与 GitHub 精确提交一致。
- 真实只读数据库 Page 组 62：总 13、旧 8、新 12、仅被封 1。
- `PagePoolRepository.list_pages(["62"])` 返回 12 个可发 Page，5 个历史
  跳过 Page 中 4 个具有候选 Token、被封 Page 为 0。
- health 同时保留 `prebuild_enabled=true`、`live_enabled=true`。
- sidecar active/running、`NRestarts=0`，七个 timer 恢复 active。
- SQLite `quick_check=ok`；历史 run 17-21 保持 8/5，不重写。
- 不调用 run-now；通过部署前后 attempt/ledger/任务快照区分自然发布增量与测试增量。

## 回滚方案

1. 停止五个运行型 timers，并确认无危险中的发布状态。
2. 将 `/opt/fb-auto-post/current` 原子切回部署前记录的旧 release。
3. 重启 `fb-auto-post-service.service`，验证 health、日志和旧口径只读查询。
4. 恢复五个 timers。
5. 不恢复 SQLite online backup，不覆盖部署后的任务、attempt、ledger、Token 或 wrapper。

## 注意事项

- 生产 live gate 已开启；切换必须短暂停止运行型 timers。
- 不通过真实 Facebook Post 验收。
- 既有冻结运行不重算，新规则由后续新 run 生效。
