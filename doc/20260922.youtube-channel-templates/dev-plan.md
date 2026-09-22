# 实现

基线 origin/codex/youtube-worker-stall-20260921 (129a75b)。线上 app/service/channels/发布 JS 与此分支一致，quick-nav 另有有效线上补丁，部署时仅插入频道导航项，不能整份覆盖。

1. 新 ChannelTemplateStore 在现有 SQLite additive 初始化，写事务比较版本并保存三项文本和操作者。
2. ChannelDirectory 提供只读身份解析，不以发布资格约束模板编辑；Workflow 暴露独立列表/读取/保存方法。
3. app 增加三个路由和按页面分支的导航鉴权，保留旧路由行为。静态新页面复用 QuickNav/UiTopbar；发布表单选择频道按次读取并覆盖非空值。
4. 后端单测、HTTP AST 合约、隔离浏览器与回滚演练；GitHub exact commit 上线。仅重启 API，保留 worker 状态。

验证：python -m py_compile；node --check；python -m unittest discover -s scripts -p 'test_youtube*.py' -q；playwright-cli run-code --filename tests/youtube_channel_templates_qa.js；预约和轮询竞态浏览器回归。
