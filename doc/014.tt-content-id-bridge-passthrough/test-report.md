# 测试报告

## 测试结论

本地、生产发布、2026-07-27 回归和 2026-07-31 目标切换验收全部通过，可以正式使用。

## 测试范围

- JavaScript 参数契约。
- Python/既有前端语法回归。
- 390×844 移动端页面交互。
- 从中间页进入真实 W2A 的导航行为。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Node URL/参数契约 | 53 项断言 | 53 | 0 | 0 |
| Python 桥接契约 | 8 项 | 8 | 0 | 0 |
| JavaScript 语法 | 1 个入口 | 1 | 0 | 0 |
| 目标落地页预检 | 1 | 1 | 0 | 0 |
| 生产真实浏览器 | 1 | 1 | 0 | 0 |

## 缺陷情况

产品代码未发现缺陷。发布过程发现并关闭 1 个 Nginx reload 就绪竞态，详见 `bugs/BUG-001.md`。

## 验证证据

- 示例链接精确生成：
  `https://www.dramawavew2a.com/ads/101/2250/view?af_dp=l9rP6ey2CB&c=TTpost&af_c_id=0001&af_adset_id=XXX`
- 入口伪造的 `af_dp/c/af_c_id` 被忽略。
- 编码值、Unicode 值与重复参数按 URL 语义保留。
- 本地页面控制台 0 error、0 warning。
- 实际 W2A 导航保留完整查询参数并解析到对应剧集页面。
- 生产 `/tt` 返回 200、无重定向、`no-store`，Nginx 和主 API 均为 active。
- 2026-07-27 再次核对线上哈希与发布提交一致，真实移动端点击仍通过。
- 2026-07-31 真实浏览器从 `/tt?af_adset_id=XXX` 搜索并点击后进入 landing `2250`，页面标题和 `content_id=l9rP6ey2CB` 对应，控制台 0 error。
- 生产 JS 来自提交 `85d9e8e3d8e3500e370e16df7dcc46ee5b93487a`，应用副本与公开副本 SHA-256 均为 `635d50a21aa69fcf68f84611e08ac4e9195957476739fcd40a5bf75a957e1a80`。

## 遗留风险

- W2A 对无效 `content_id` 可能展示默认剧；未来若要前置判错，必须增加精确 resolver API 并比较实际解析 ID。
- URL 会进入 Nginx access log，不得把敏感个人信息作为透传参数。

## 发布建议

发布通过。可将 `https://ai.yingliangads.com/tt?<附加参数>` 用作 TikTok 主页固定入口。
