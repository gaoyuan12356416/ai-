# 2026-08-12 片尾模板未生效事件

## 影响

- TT 自动模板 `2` 的版本 `1`、`2` 都冻结成 `video_template=random_overlay`。
- 手动运行 `27` 的任务 `149`、`150`、`151` 因此按“随机排重、无片尾”制作并发布。
- 三个任务均已确认发布且 `unknown_outcome=0`，不得通过重跑或改写历史账本补发。

## 根因证据

1. 生产数据库中的模板版本 `2/v2` 明确保存为 `random_overlay`，不是 `direct_outro`。
2. 生产 Nginx 日志显示操作人的 Chrome 在 14:26 请求过未版本化的
   `/tt-auto-publish-template.js`；视频模板选择功能在 15:01 部署。
3. 15:20 创建模板、15:22 更新模板时，HTML 和 API 均有新请求，但浏览器没有再次请求该 JS。
4. 部署前旧 JS 的保存请求不包含 `video_template`；当时后端对缺字段请求静默兼容为
   `random_overlay`。因此页面 HTML 已出现新下拉框时，旧缓存 JS 仍可把选择丢掉并成功保存。

## 修复

- 编辑页以版本化 URL 加载 `tt-auto-publish-template.js`，使新 HTML 不会复用旧保存逻辑。
- 新建/更新 API 必须显式包含 `video_template`。旧缓存请求返回
  `tt_auto_video_template_required` / HTTP 409，不再静默降级。
- 历史数据库版本缺字段时仍按 `random_overlay` 读取和执行，不做回填，不改变冻结任务。

## 验收边界

- 使用隔离数据库验证缺字段写请求被拒绝且模板数不变。
- 使用显式 `direct_outro` 验证新版本保存和执行路由合同。
- 生产部署只更新 TT auto CPU 服务和模板编辑静态页；不重启 GPU，不触发 run-now，
  不创建真实 TikTok Post。
