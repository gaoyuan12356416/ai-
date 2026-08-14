# 部署文档

## 变更内容

账号剧语言、三条自动发布语言路由、同语言 Premium 中继、管理 UI、迁移脚本与测试。

## 配置项

无新增环境变量。沿用账号 SQLite、X Post SQLite、X Auto SQLite 和现有 systemd 配置。

## 数据库变更

- `x_authorized_account.drama_language`，默认 `en`。
- `x_post_queue.account_drama_language`，默认 `en`。
- `x_post_queue.account_drama_language_frozen`，默认 `0`；只有新语言路由队列写入 `1`。
- 生产账号 19、20 显式设为 `ja`；其余账号保持 `en`。

## 部署步骤

1. 合并并从 GitHub 拉取已验证提交，创建新的不可变 release 目录。
2. 停止 `x-auto-post-service`、`x-post-automation` 及相关 claim/runner 定时器，确认无正在执行的发布任务。
3. 对账号/X Post SQLite 做完整带时间戳备份；若两者是不同文件，必须分别备份。
4. 使用 `scripts/migrate_x_account_drama_languages.py` dry-run 核对 19、20。
5. 以带时间戳且不存在的路径执行 `--apply --backup`，显式设置 `19=ja`、`20=ja`。
6. 切换 `current` symlink，部署主应用代码与静态页面。
7. 依次启动 sidecar、X Auto、API 和定时器。
8. 不手动触发 publish/run-now，不创建真实 X Post。

示例（路径以生产实际值为准）：

```bash
python3 scripts/migrate_x_account_drama_languages.py \
  --db /var/lib/x-post-automation/accounts.sqlite3 \
  --set 19=ja --set 20=ja

python3 scripts/migrate_x_account_drama_languages.py \
  --db /var/lib/x-post-automation/accounts.sqlite3 \
  --set 19=ja --set 20=ja --apply \
  --backup /var/lib/x-post-automation/backups/accounts.before-language-YYYYMMDDHHMMSS.sqlite3
```

## 验证步骤

- 读取 schema，确认账号表新增语言列、队列表新增语言与冻结标记列，账号 19/20=`ja`，其他账号=`en`，历史队列冻结标记=`0`。
- 查询账号接口，确认 DTO 不含 token 且返回 `drama_language`。
- 对素材和短剧执行离线/validate-only 选择测试，确认 en/ja 路由。
- 查询 X Auto 模板：历史 `jp` 模板账号 19/20 校验通过。
- 检查服务、timer、日志和队列/ledger 计数未出现意外增长。
- 等待自然调度的 `no_due` 或新任务预检证据；不得以真实发帖验证。

## 回滚方案

1. 停止新版本服务和相关定时器。
2. 将 `current` symlink 切回部署前 release，并从部署备份恢复主应用和静态文件。
3. 默认保留当前 SQLite：旧代码会忽略新增列，完整覆盖数据库可能丢失部署后的真实发布历史。若业务同时要求撤销账号语言，先只读核对部署后增量，再审慎把账号 19、20 改回 `en`；不要删列。
4. 启动旧服务并恢复原 timer 状态，只读核对队列、日志、未知结果和 X Auto ledger。

## 注意事项

- 先备份再迁移，备份路径必须与源库不同且不能已存在。
- 不打印 token、请求体凭证或 OAuth 文件内容。
- 若发现 `post_creating`、`repost_creating` 或 unknown outcome，停止部署并按现有恢复流程处理。
- 随机计划和已冻结队列不改写；上线后的账号语言只用于新任务。

## 2026-08-14 生产部署结果

- GitHub 分支：`codex/x-account-language-routing-20260814`；生产代码 release：`7ed5f203e028129f88afbc675da9237326bfd364`。
- 当前路径：`/mnt/data-disk/x-post-automation/releases/7ed5f203e028129f88afbc675da9237326bfd364`；部署前 release：`d716f8de58a933dc592c7b56c17bb693e8f500bc`。
- 完整回滚包：`/mnt/data-disk/x-post-automation/backups/20260814T174319+0800-x-account-language-7ed5f20`。在线数据库、Token 元数据、主应用、静态文件、环境文件、systemd 配置、迁移前后证据和校验清单均已留存；Token 内容未输出，哈希、权限和属主未变化。
- 迁移后 17 个账号中，账号 19、20 为 `ja`，其余 15 个为 `en`。历史 254 条队列全部保留 `account_drama_language_frozen=0`，未改写既有 X Auto 模板、任务、随机计划和发布历史。
- 主 API、X Sidecar、X Auto 均为 active；健康门禁全部打开。主应用与 Nginx 静态文件哈希和 release 一致，公共页面返回 200，新语言写接口在匿名请求下返回 401。
- 模板 2（`en`）和历史模板 3（`jp`，按 `ja` 比较）只读预览均返回 200、`reserved=false`；当前候选结果是 `x_auto_no_eligible_material`，不是账号语言不匹配。
- 自然定时器观察得到素材调度 `no_due`、claim 数量 0、手工任务 `no_pending`，X Auto scheduler/runner 自然成功。观察前后 X Post 队列/日志/已发布/确认 Post ID/未知结果为 `254/244/242/242/0`，X Auto Run/Task/Ledger 为 `9/19/4`，均无增长；未触发 `run-now`，未创建真实 X Post。
- 两次早期切换未留下在线变更：`4230981` 在 release 演练阶段因迁移脚本导入路径失败而停止，`450316a` 因 Sidecar systemd 启动导入路径失败而自动回滚。对应失败 release 与备份均已标记保留；修复后由 `7ed5f20` 完成正式切换。

生产回滚时，先停相关 timer/service，把 `current` 切回上述 `d716f8d...` release，并从正式回滚包恢复主应用与静态文件；数据库默认保留当前版本。恢复服务和原 timer 状态后，必须重新核对健康、队列/日志/未知结果、X Auto ledger 和 Token 哈希。
