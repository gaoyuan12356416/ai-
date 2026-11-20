# FB Page 列表局部授权核验（2026-11-20）

Page 管理过去对同租户约 3910 个配置 Page 按 200 ID 分片调用远程 SQL，任一分片 source_busy 都会导致整表 503。默认 GET `/api/fb-video-publish/pages` 现在仅查询同租户 SQLite 配置与可选的只读名称缓存，不将缓存当授权依据。`purpose=publish` 同样只返回本租户已配置项目。

授权使用同一路由 `?verify_ids=id1,id2,...`，严格限制 1–8 个不重复、最长 31 位数字 ID。先验证这些 ID 均属于本租户有效配置，再查询远程授权。源记录不存在仍返回配置 ID、0 个有效授权和明确不可发布原因。源繁忙只影响此次核验。

管理页每个列表 generation 只尝试每个显示 ID 一次，当前 20 行顺序分 8/8/4 核验；翻页核验新项，刷新重新尝试。旧 generation 响应被忽略。只合并名称和授权展示字段，不覆盖文案、语言、版本和创建人。局部失败保留表格、搜索及创建/Excel 入口。发布页未知授权项可选并标注“发布时核验”，实时 resolve/preflight、提交复核及 worker 校验保持原逻辑。原共享 Page 手动发布规则和创建人历史保持。

## 验证

代码基线仓库未包含全部组合运行依赖，Python 测试需在当前完整运行态的隔离副本 overlay 后运行：

```
python3 -B -m unittest tests.test_fb_shared_manual_pages tests.test_fb_page_local_list -v
node --check static/fb-video-publish.js
```

7 项后端测试覆盖大列表 source_busy 可用、租户隔离、名称缓存缺失/损坏、8 ID 上限、缺失授权记录、局部失败以及原共享发布 3 项。`tests/test_fb_page_local_list_ui.js` 是 Playwright CLI 的完全模拟传输浏览器验证，运行 `playwright-cli run-code --filename tests/test_fb_page_local_list_ui.js`；验证当前20项、8/8/4、翻页去重、刷新重试、旧响应和配置合并，以及未知授权可选进入预检。无需创建线上发布批次或实际发帖。

生产只读验证脚本 `scripts/verify_fb_page_local_list.py --output /mnt/data-disk/.../verification.json` 仅使用接续任务给定的邱心敏/朱锦茵普通用户 trusted actors，在内存读取内部 bearer，不绕过主网关的用户权限逻辑。它证明内部普通用户业务路径，不代表实际浏览器 cookie 登录验收。

## 部署与回滚

先 commit/push，再从 GitHub 精确取回该 commit 作为部署源，运行 `scripts/deploy_fb_page_local_list.py prepare --folder /mnt/data-disk/fb-page-list-query-fix-20261120/release`。控制器复用 `/mnt/data-disk/fb-shared-manual-pages-20261120/source` 已部署的 drain/switch helpers，并记录其哈希。复制完整当前组合运行态，只 overlay service.py、JS、相关测试和三份 HTML 脚本版本；主 API备用 service.py、主静态和 nginx 静态有逐文件备份，HTML只修改版本号保留其他差异。所有基线和当前目录指纹必须一致，冲突则停止重建候选。

使用 systemd-run --no-block 执行 apply，沿用 owner_active 和 scheduler health snapshot/restore；只切 FB owner，主 API和画像服务无需重启。不增加 SQL 槽位，不绕过 FIFO，不改任何 ledger schema/data。manifest 和旧完整运行目录均保留。回滚使用同一脚本 `rollback --folder ...`，同样排空真实在途工作和恢复原调度状态；不会恢复旧 ledger、清防重或重发历史。
