# X 自动模板读取账户剧语言

模板不再填写剧语言。同一模板可选择不同语言的 X 账户；预览和新任务按每个账户当前设置的 `drama_language` 筛选剧、素材。账户设置未填时沿用账户契约的 `en` 默认值，历史 `jp` 规范化为 `ja`。

新建、编辑请求省略 `language`。旧编辑器提交的 `language` 被忽略，新版本配置保存 `language_source=account`。旧模板无需批量改写；后续生成的新任务也读取账户语言。

正式任务冻结账户身份、账户语言和语言来源。重入同一任务保留冻结内容，发布前账户语言漂移继续拒绝。历史模板版本、已有任务、素材预留、发布记录不回写。账户验证失败只记录失败任务，语言未知时留空，不推测语言。

## 验证

```powershell
python -m unittest discover -s scripts -p 'test_x_auto*.py' -q
python -m unittest scripts.test_x_account_language_routing scripts.test_x_post_auto_template_bridge -q
git diff --check
```

覆盖缺省语言请求、多语言账户、旧客户端参数、逐账户预览、账户语言别名、任务幂等、旧模板的新任务、历史任务保留、无效/不可用账户和存储层防覆盖。

Playwright 离线浏览器验证：英文与日文账户可以同时选中，停用账户保持禁选，摘要显示各账户语言，保存请求没有 `language`，成功回到列表；HTTP 409 保留编辑页、输入和账户选择。

## 部署与回滚

先提交并推送 GitHub，再用 `scripts/install_x_auto_account_language.py --commit <完整提交> --expected-release <已核实当前版本目录>` 从 GitHub 读取精确文件。脚本在真实数据盘构建原线上版本的复合发布目录，执行 Linux 回归，备份两套 SQLite、静态文件和非敏感 Token 哈希/权限。

暂停并恢复 X Auto 原本启用的定时器，等待请求结束，取得共享发布锁与调度锁，重启独立的 `x-auto-post-service.service`。只替换 X Auto 的 core/service/validation 和编辑/列表静态文件；同步静态文件到独立发布目录、主 API 静态目录和 Nginx。主 API、X OAuth/发布服务及其他发布定时器保持原状态。

上线后检查服务健康、文件哈希、全量 Auto 状态和 X 发布账本指纹、Token 哈希/权限、定时器状态、公网页面缓存版本及匿名写保护。不创建测试模板、任务、Post 或 Repost。

回滚代码与四个静态文件：

```bash
python3 <上线版本>/scripts/install_x_auto_account_language.py --rollback <维护备份目录>
```

回滚保留现有数据库和 Token；不恢复旧数据库覆盖后续账本。

## 2026-10-09 上线验收

- 代码分支：`codex/x-auto-account-language-20261009`，GitHub 已推送并由服务器读取精确代码提交 `c243817b1fd77e0de42f340f2fa6ebb1dddb5c56`。
- 服务器：`43.166.187.96`；上线目录 `/mnt/data-disk/x-post-automation/releases/c243817b1fd77e0de42f340f2fa6ebb1dddb5c56-auto-account-language`，由 `/opt/x-post-automation/current` 指向。
- 维护备份：`/mnt/data-disk/x-post-automation/maintenance/20261009-auto-account-language-c243817b`，含两套 SQLite 在线备份、主 API/Nginx 原静态文件与指纹清单。原版本为 `/mnt/data-disk/x-post-automation/releases/02ab0404adbdd1c974ee0f5547bb19ac95869a80-drama-save`。
- Linux 回归 176 项全部通过；本地回归 175 项通过、1 项 Windows 环境条件跳过。Python 编译、Node 语法检查、`git diff --check` 通过。
- Playwright 创建/历史模板编辑验证：英文和日文账户同时选中，停用账户禁选；请求没有 `language`；成功返回列表；409 保留编辑页、输入、选择和 `expected_version`。
- 独立 Auto 服务健康。四个公网文件 HTTP 200 且与 GitHub 代码提交逐字节一致；匿名创建模板返回 `401 auth_required`。
- 线上模板均返回 `language_source=account`，旧版本配置的语言原值保留。只读预览按日文账户设置的 `ja` 成功选择；英文账户按 `en` 筛选，返回现有规则下 `x_auto_no_eligible_material`。两次预览均 `reserved=false`，没有新建任务或发布。
- 维护锁期间所有 Auto 状态、X 发布账本和 Token 哈希/权限一致；Auto 定时器恢复原状态，已停用的其他 X 发布定时器保持停用。后续预览核对中发现一个模板范围外账户在维护结束后完成 OAuth 重新授权，未回滚其凭证；模板、任务、素材预留、发布账本的指纹仍一致。

本次确切代码回滚命令（自动等待 Auto 请求结束并保持原定时器状态）：

```bash
python3 /mnt/data-disk/x-post-automation/releases/c243817b1fd77e0de42f340f2fa6ebb1dddb5c56-auto-account-language/scripts/install_x_auto_account_language.py --rollback /mnt/data-disk/x-post-automation/maintenance/20261009-auto-account-language-c243817b
```
