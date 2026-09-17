# 测试报告

最终本地164项通过：主桥接17、UI17、Sidecar28、YouTube HTTP21、工作流45、素材筛选6、X应用契约30。独立评审问题均已修复；生产源视频只读验证、真实账号选项与宏预览均通过。完整证据和生产验收见release-result.md。

本地组合基线：旧x_posts/service.py不接受当前线上OAuth使用的access_token_provider，X账号全套有1项旧依赖失败。以原始生产OAuth sha7ce8ee323de50de66ca93389650c9e99175863fa4a71430e455b9d1614080b6e离线复现相同TypeError。发布复制完整当前线上Sidecar，未下发旧依赖；CPU以真实上线组合回归X账号68/68及分享28/28全部通过。
# 2026-09-17 短链宏增量

- Bridge 19/19、UI 17/17 通过；Python 编译、Node 语法、`git diff --check` 通过。
- 使用冻结任务短链的 options/preview/create 内容一致，短链与视频原链接各按 23 权重计数。测试禁止再次调用短链生成器。
- 缺少短链时，仅包含 `{short_url}` 的文案被阻止，原默认描述仍可用。UI 验证新按钮可见、插入保留光标及选中位置。
- 本次无真实 X 发帖，生产证据见 `short-url-macro-20260917.md`。

# 2026-09-17 播放器校验修正

- 源页面从 CPU 和本机读取结果一致：用户反馈的视频是 summary_large_image，先前成功样例是 player；反馈视频的 YouTube API 状态为 public/processed/embeddable=true。因此仅检查公开视频和嵌入权限不够。
- 卡片检查 4 项、bridge 20 项、UI 17 项。覆盖匹配/错误视频、图片卡片、元数据冲突、超时、跳转、体积限制、提交前重新验证及失败零入队；现有 UUID 重放不重复检查或发布。
- 描述省略视频宏时自动将直链放到其他链接前；显式将其放在其他链接之后会报错。保留短链和所有自定义文案。实际发送与预览使用同一渲染函数。
- 用户明确要求删除的测试帖已定点删除；X 返回 deleted=true，后续只读查询返回 resource-not-found。保留原分享台账，未创建替代帖子。详细生产证据见 `player-card-fix-20260917.md`。
# 2026-09-17 播放器卡片列表标记增量

本地已通过：标记后端 14、YouTube 工作流 45、分享桥接 20、公共卡片解析 4、HTTP 21、前端 Node 19，共 123 项。Python 编译、Node 语法与 git diff 检查通过。未进行真实平台发布。

浏览器连接连续两次因 request-header policy 初始化失败，未完成实际浏览器视觉验收；前端状态/逃逸/过期降级已由 Node 用例验证。CPU 104 项 Python 用例通过，生产真实任务接口、公网文件、33 个绿色标记、两项受限及其他状态、列表不阻塞、since/匿名鉴权、服务进程与回滚条件均已核对，详见 player-badges-20260917.md。
