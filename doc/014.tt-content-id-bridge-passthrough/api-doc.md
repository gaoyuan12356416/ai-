# API 文档

## 接口列表

本需求不新增后端 API。

## 请求/响应

公开页面：

`GET https://ai.yingliangads.com/tt?<附加查询参数>`

页面在浏览器内生成：

`GET https://www.dramawavew2a.com/ads/0/2049/view?af_dp=<content_id>&c=TTpost&af_c_id=0001&<附加查询参数>`

## 错误码

无自定义 HTTP 错误码。输入校验错误在页面内提示。

## 兼容性说明

- 支持现代 TikTok 内置浏览器、Chrome、Safari 中的 `URL` 与 `URLSearchParams`。
- 三个核心参数不可由入口 URL 覆盖。
- 超限或非法附加参数将被忽略。
- `/tt` 为 Nginx 精确匹配，不重定向到带斜杠路径。
