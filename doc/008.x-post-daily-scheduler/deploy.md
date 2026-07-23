# 部署文档

## 变更内容

- 部署 X 日批次/全局去重/日志查询 release。
- 增量迁移 X Post SQLite。
- 部署管理员日志页面和导航。
- 安装 `x-post-daily.service` / `x-post-daily.timer`，默认北京时间 10:00。

## 配置项

- `/etc/x-post-automation.env`：现有 X OAuth/发布配置保持 root-only，不向 runner 暴露 Refresh Token。
- `/etc/x-post-daily.env`：只读 MySQL 63350、内部 loopback URL/bearer、固定三个账号 ID、候选上限、开始日期。
- `X_POST_DAILY_ACCOUNT_IDS` 必须恰好三个不同正整数。
- `X_POST_DAILY_START_DATE` 首次部署设置为次日。

## 数据库变更

- `x_post_daily_run` 新表。
- `x_post_queue` 增量列与唯一索引。
- 迁移前对生产副本运行重复检查；任何冲突中止部署。
- 发布成功后回滚只回代码/unit，不删除新表或恢复旧数据库覆盖真实日志。

## 部署步骤

1. 验证代码/Skill 两个工作树状态和 GitHub 精确 commit。
2. 验证 `/mnt/data-disk` UUID、空间、权限。
3. 在线备份 SQLite，备份 Token 目录 hash/mode、env、unit、Nginx/静态页面、当前 release 和 timer 状态。
4. 在备份数据库副本迁移并运行全部测试。
5. 从 GitHub checkout 精确 commit 到新 release，验证 hash 与测试。
6. 停止/迁移/重启仅 `x-post-automation.service`；主 API 只有在 live composite 基线精确匹配时才部署和窄重启。
7. 部署静态日志页/导航，验证管理员鉴权和 no-store。
8. 安装 timer/service，先运行 `--dry-run` 和 start_date 门禁，再 `enable --now` timer。
9. 核对下一次触发时间、服务状态、journal 脱敏和 DB 唯一约束。

## 验证步骤

- 全量 unittest、py_compile、JS syntax、`git diff --check`。
- local/public Sidecar health 200，公网 internal 404。
- 管理员日志页面/API 200；普通用户/API Token 403。
- 生产副本与 live 迁移后旧行/Token hash/mode 不变。
- timer active/waiting，next trigger 为次日 10:00 CST；部署日补跑被 start_date 拦截。
- dry-run 只产生计划预览，不创建 queue/short link/Post。

## 回滚方案

1. `systemctl disable --now x-post-daily.timer`，停止新调度。
2. 保留新日志/短链/当前 Token；将稳定 release 链接切回上一个精确 commit，恢复 unit/静态备份并窄重启。
3. 已产生真实日志后不恢复部署前 SQLite，不删除 `x_post_daily_run`/queue/log。
4. 恢复静态页面/导航时保留现有 Post 的 `/s2l/<log_id>.html`。
5. 若首次调度尚未发生且迁移无真实新记录，可在人工核对后用在线备份回滚数据库。

## 注意事项

- 不在部署时手工执行三账号真实发布；首个正式批次由次日自然 timer 触发。
- timer 启用前必须确认三个账号当前 active/publish eligible。
- 不输出或提交任何真实密码、OAuth Token 或内部 bearer。
