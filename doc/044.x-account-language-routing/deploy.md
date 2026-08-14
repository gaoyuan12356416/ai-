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
2. 将 `current` symlink 切回部署前 release。
3. 恢复迁移脚本生成的完整 SQLite 备份；不得仅手工删列。
4. 恢复部署前静态文件/主应用版本。
5. 启动旧服务并只读核对队列、日志、未知结果和 timer。

## 注意事项

- 先备份再迁移，备份路径必须与源库不同且不能已存在。
- 不打印 token、请求体凭证或 OAuth 文件内容。
- 若发现 `post_creating`、`repost_creating` 或 unknown outcome，停止部署并按现有恢复流程处理。
- 随机计划和已冻结队列不改写；上线后的账号语言只用于新任务。
