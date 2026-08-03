# 部署记录（GitHub-first）

## BUG-005 增量状态（已部署并通过生产只读验收）

- 目标：发布任务主表通过只读 `/tasks` 统一显示自动/排期 queue 与立即测试 direct-test；旧 `/queue` 保持原合同。
- GitHub 代码 commit：`f91a3e1ae82c9843b37145d60c3fe5c188a8fea3`；分支 `codex/tt-post-direct-multi-config-20260803` 的远端引用已核对一致。
- CPU release：`/opt/tt-post/releases/f91a3e1ae82c9843b37145d60c3fe5c188a8fea3`；回滚 release：`/opt/tt-post/releases/9fd0f99843d45269a5f2e4f0c7028c56321e427c`。
- SQLite：`/mnt/data-disk/tt-post-publisher/tt-post.sqlite3`；最终 online backup：`/mnt/data-disk/tt-post-publisher/backups/20260803T104443Z-9fd0f99-to-f91a3e1-bug005-final`。
- 三份静态页及公网响应 SHA-256 均为 `57f26de36daec7ac079b5965c3277479485601f069c79870aa000bfb69d209c8`。
- 生产纯 GET `/tasks` 返回 total=5、published=4、scheduled/processing/needs_review=0；`direct_test:1` 为素材 `5837129`、账号 `640`、状态 `published`、标签“立即测试”；旧 `/queue` 仍为 4 条。
- 验收前后 10 张 TT 表的逻辑指纹均为 `7aadf756ac4b733fa3109bab130d74863ac9d4b8d5e86fe74a48dd6773d8feb2`，`integrity_check=ok`；已有 publish ID `v_pub_url~v2-1.7669738344867448839` 未变。
- sidecar、主 API、runner/prepare timer/path 均正常；登录态 Chrome 已刷新并看到“共 5 条 / 已发布 4 条”和“立即测试 1”行。验收未调用任何 POST、未保存配置、未创建 TikTok Post、未消费素材池，GPU 未改动或重启。
- 第一次切换因远程验收脚本直接比较中文标签的传输编码而触发自动回滚；旧 release、三份静态页、服务及数据库均核对恢复。修正为 Unicode escape 断言后重新部署并通过，首次备份保留在 `20260803T104225Z-9fd0f99-to-f91a3e1-bug005`。

## 2026-08-03 027 基线生产结果（历史）

- GitHub 分支：`codex/tt-post-direct-multi-config-20260803`；生产代码 commit：`9fd0f99843d45269a5f2e4f0c7028c56321e427c`，远端引用已核对一致。
- CPU 主机：`43.166.187.96`；当前 release：`/opt/tt-post/releases/9fd0f99843d45269a5f2e4f0c7028c56321e427c`。
- 回滚 release：`/opt/tt-post/releases/282eb914172531bd55500b65539d5715a282e5bc`。
- SQLite：`/mnt/data-disk/tt-post-publisher/tt-post.sqlite3`；online backup：`/mnt/data-disk/tt-post-publisher/backups/20260803T085637Z-282eb91-to-9fd0f99-direct-multi`。
- 隔离 DB-COPY 连续初始化两次通过；`PRAGMA integrity_check=ok`；仅新增 `tt_post_auto_publish_config`、`tt_post_direct_test` 和约定的 6 个索引。
- 三份生产静态页 SHA-256 均为 `b0e9ac232a1a4548a201858b7e490a74474d5e449d671df481974abbf2e95de9`；公网页面返回 200 且 hash 一致。
- `tt-post-service.service`、`drama-material-api.service`、runner/prepare timer/path 恢复正常；17:00 自然 tick 为 0 个自动任务、0 个立即测试任务。
- 只读验收前后 schedule/pool/queue/run/intake 统计一致；新配置表和 direct-test 表均为 0 行，没有保存配置、消费素材或创建 TikTok Post。
- GPU release/profile/env/ledger 未修改，GPU 服务未重启。

## 发布前提

- 99/99 计划用例通过，其中新增 T01-T12 必须有独立结果；9 个真实测试脚本全部通过，P0/P1 开放缺陷为 0。
- 变更已提交并推送 GitHub；记录精确 commit，生产只能拉取/checkout 该 commit，不直接复制本地源码。
- 明确 CPU 主机、部署路径、sidecar/app/Nginx 静态页路径、service/timer 名称、SQLite 绝对路径和 GPU 服务现状。
- `.env`、Token、COS Secret、数据库副本和生产凭据不得进入 Git/GitHub。
- 生产验收只读，不创建真实 TikTok Post，不保存 auto-config，不调用内部 publish。

## 本地门禁

```powershell
git status --short
git branch --show-current
git remote -v
python -m py_compile features/tt_posts/core.py features/tt_posts/service.py scripts/tt_post_prepare_runner.py scripts/tt_post_runner.py app.py
python scripts/test_tt_account_settings_ui.py
python scripts/test_tt_gpu_worker.py
python scripts/test_tt_post_direct_config_core.py
python scripts/test_tt_post_links.py
python scripts/test_tt_post_pool_ui.py
python scripts/test_tt_post_prepare_runner.py
python scripts/test_tt_posts_app_contract.py
python scripts/test_tt_posts_core.py
python scripts/test_tt_posts_service.py
git diff --check
```

不得引用仓库中不存在的测试文件。提交前确认 `migration.sql` 只是评审镜像，不作为生产手工 SQL 执行。

## DB 副本迁移门禁

1. 用 SQLite online backup 创建隔离 DB-COPY，确认任何 runner/service 都不连接该副本。
2. 以候选 commit 启动初始化两次。
3. 验证 `PRAGMA integrity_check=ok`，旧 schedule/pool/queue/run 行与索引 0 diff。
4. 新 schema 只包含：
   - `tt_post_auto_publish_config`；
   - `tt_post_direct_test`；
   - direct-test prepare/publish/material 三个普通索引，以及 active-material、publish-id、short-link 三个 partial unique 索引。
5. 明确不存在 `tt_post_direct_test_event`、`tt_post_auto_due` 和 direct-test account index。

## GitHub 发布

1. 只暂存 027 对应代码/文档，排除无关 dirty files 和任何 Secret。
2. 提交并记录 commit SHA，推送目标分支。
3. 验证远端：`git ls-remote <remote> <branch>` 指向该 SHA。
4. 未验证 push 时不得称为“已同步 GitHub”。

## 服务器部署顺序

1. 只读记录服务器 repo 状态：路径、branch、remote、`git status --short`、当前 commit。
2. 记录 service/timer/Nginx 状态和最近业务日志；保存当前 release commit 作为回滚点。
3. 对非 Git 管理的 unit/Nginx/静态副本做带时间戳备份；SQLite 仅做 online backup 留作灾难恢复，不作为普通回滚覆盖源。
4. `git fetch --all --prune`，checkout 精确候选 commit 或 `git pull --ff-only` 到已验证分支。
5. 重启前运行 Python 编译、必要的无网络测试、Nginx 配置测试和三份静态页 SHA 比对。
6. 只重启/重载受影响的窄服务；不使用宽泛 `pkill`，不改 GPU release/profile/env/ledger。
7. 验证 service/timer active、health 正常、日志无 schema/route 错误。
8. BUG-005 仅涉及 CPU read model、同源代理和静态页；除非实现 diff 另有必要，不重启 runner/prepare timer 或 GPU 服务。

## 只读生产验收

1. 打开页面并只执行 GET：accounts、auto-config、material-pool、direct-tests、tasks、旧 queue。
2. 确认账号列表显示 `auto_publish_selected` 与 `active|paused|attention_required|not_selected`；配置响应顶层为 `item`。
3. 确认素材只显示 `published|unknown|unpublished`，无 `processing`。
4. 检查浏览器 Network：无 `POST /auto-config`、`POST /material-pool`、`POST /test-publish`、`POST /run-now` 或 `/internal/*`。
5. 前后只读比较 config/schedule/pool/queue/run/direct-test 行与状态、GPU ledger 文件数/hash、已知 TikTok Post ID，要求完全一致。
6. 用生产已有 queue/direct-test 事实核对 `/tasks` 的类型、summary、筛选与分页；不为验收创建新任务。
7. 相同数字 ID 如无生产 fixture，只在临时 DB 自动化验证；生产不得为制造 fixture 写入数据。

## 发布后报告必须包含

- GitHub commit SHA、branch 和远端验证；
- CPU host、部署路径、DB 绝对路径；
- 部署前 commit/备份/回滚点；
- 本地与服务器验证命令/结果；
- service/timer/Nginx restart/reload 结果；
- 生产只读 0 副作用证据；
- 精确回滚步骤。

BUG-005 普通回滚只切回上一 CPU release 和三份静态页；不恢复 SQLite backup，不删除 direct-test/queue 历史，不改 GPU ledger/COS/短链。回滚后旧 `/queue` 和原 direct-test 最近任务区应继续可读。

2026-08-03 BUG-005 增量仅执行版本化 CPU 代码/静态页切换和必要的窄服务重启；未执行 schema 变更，验收阶段未调用任何业务写接口。
