# 测试报告

## 当前结论

本地自动化测试共执行 229 项，229 项全部通过。生产 CPU release 已完成关闭态部署与真实浏览器验收；没有保存任何真实账号设置、创建发布任务或发起 TikTok Post。

## 自动化测试

| 测试集 | 通过 | 失败 |
| --- | ---: | ---: |
| TT Core | 34 | 0 |
| TT CPU Service / HTTP / Runner | 40 | 0 |
| TT GPU 回归 | 25 | 0 |
| TT App contract | 10 | 0 |
| TT 发布池 UI | 11 | 0 |
| TT 个号管理 UI | 9 | 0 |
| **TT 小计** | **129** | **0** |
| X 发布池回归 | 72 | 0 |
| 素材状态回归 | 28 | 0 |
| **总计** | **229** | **0** |

## 已验证重点

- 第四张账号设置表可从旧库安全创建。
- 首次保存、更新、版本冲突和缺少版本均 fail-safe。
- 商业披露一致性、隐私范围和互动能力由服务端校验。
- 未配置账号不能排期且不会开始 GPU 制作。
- 遗留客户端字段不能覆盖账号配置。
- 配置变更不改写历史任务，同幂等键重放不重复制作。
- 两个页面无 Token 字段、无 HTML 注入，内联 JavaScript 可解析。
- Direct Post 门禁默认行为未改变。
- Python 编译、`quick-nav.js` 与两个页面内联 JavaScript 语法检查通过。
- `git diff --check` 通过。

## 生产验收

- GitHub 提交：`9fd643137dd8d33e2ec8a804b333d5ec0584bbde`。
- CPU release：`/opt/tt-post/releases/9fd6431`，`/opt/tt-post/current` 已指向该目录。
- 生产 TT 测试在服务实际临时目录 `TMPDIR=/run/tt-post` 下执行，129/129 通过。首次直接使用 Linux 默认 `/tmp` 时有 5 项仅因生产 runner 的临时目录约束失败，改用正式运行目录后全部通过，无业务断言失败。
- `tt-post-service.service`、`tt-post-runner.timer`、`drama-material-api.service`、X 发布服务及两个 X timer 均为 `active`；runner 最近结果为 `success`。
- SQLite `PRAGMA integrity_check=ok`，四张 `tt_post_*` 表存在。
- 生产复核时账号配置数为 `0`、发布队列数为 `0`。
- 三重门禁 `TT_POST_LIVE_ENABLED`、`TT_POST_DIRECT_AUDIT_APPROVED`、`TT_POST_URL_PROPERTY_VERIFIED` 均为 `0`。
- Chrome 登录态真实验收：
  - 个号管理页返回 18 个可用账号，已配置 0、待配置 18。
  - 只读选择账号 640 后，成功实时获得三种隐私范围以及评论、Duet、Stitch 能力；未点击保存。
  - 发布池选择同一账号后只显示“该账号尚未配置”和“前往管理”，不存在账号级设置编辑控件，保存任务按钮保持禁用。
- 本次浏览器与服务器验收未填写素材 ID、未勾选 Music Usage Confirmation、未保存账号设置、未创建队列，因此未触发 GPU 制作或 TikTok 发布请求。
