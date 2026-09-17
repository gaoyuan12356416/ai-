# 测试报告

最终本地164项通过：主桥接17、UI17、Sidecar28、YouTube HTTP21、工作流45、素材筛选6、X应用契约30。独立评审问题均已修复；生产源视频只读验证、真实账号选项与宏预览均通过。完整证据和生产验收见release-result.md。

本地组合基线：旧x_posts/service.py不接受当前线上OAuth使用的access_token_provider，X账号全套有1项旧依赖失败。以原始生产OAuth sha7ce8ee323de50de66ca93389650c9e99175863fa4a71430e455b9d1614080b6e离线复现相同TypeError。发布复制完整当前线上Sidecar，未下发旧依赖；CPU以真实上线组合回归X账号68/68及分享28/28全部通过。
# 2026-09-17 短链宏增量

- Bridge 19/19、UI 17/17 通过；Python 编译、Node 语法、`git diff --check` 通过。
- 使用冻结任务短链的 options/preview/create 内容一致，短链与视频原链接各按 23 权重计数。测试禁止再次调用短链生成器。
- 缺少短链时，仅包含 `{short_url}` 的文案被阻止，原默认描述仍可用。UI 验证新按钮可见、插入保留光标及选中位置。
- 本次无真实 X 发帖，生产证据见 `short-url-macro-20260917.md`。
