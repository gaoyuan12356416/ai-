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
