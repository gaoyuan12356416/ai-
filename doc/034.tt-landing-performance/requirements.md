# 034.tt-landing-performance 需求与技术设计

## 背景

生产页面 `https://ai.yingliangads.com/tt` 当前先输出英文 HTML，随后等待
`/tt-drama-code-search.js` 下载、解析和执行后才读取浏览器语言并替换文案。
2026-08-06 用户录屏中，从英文页面出现到中文和 Featured 卡片出现约
10.9 秒。只读冷启动复测中，正确中文在导航后约 6.83 秒才出现。

当前 HTML 约 19.6 KiB、主 JS 约 106.1 KiB、全语言 Featured JSON 约
24.3 KiB，三者线上均未压缩；主 JS 还是 `Cache-Control: no-store`。
生产服务器本机读取这些静态文件的 TTFB 约 6--7 ms，说明主要瓶颈是
网络传输、资源缓存和前端初始化顺序，而不是 Redis、SQLite 或搜索接口。

## 目标

1. 页面第一次绘制即使用浏览器首选的受支持语言，不再出现长时间英文闪屏。
2. 显著缩小冷启动传输量，并让重复访问复用版本化静态资源。
3. Featured 只下载当前语言的五条记录；封面使用本地缓存缩略图并渐进加载。
4. 搜索输入、短码解析和 W2A 跳转不等待 Featured 或封面。
5. 保持 `/tt`、`/tt-code`、`{code}`、发布归因、Redis/SQLite 和 W2A 跳转契约不变。

## 范围

### 包含

- 按请求 `Accept-Language` 选择预生成的语言 HTML，无法识别时返回英文。
- 有效的服务端 locale 标记作为首屏权威语言；仅旧/直连 HTML 缺少有效标记时
  才使用 `navigator.languages` 兜底，避免延后 JS 再次切换可见语言。
- 内容哈希主 JS、不可变缓存和 HTML/JS/JSON 的 gzip 压缩。
- HTML 内置五张不可点击的 Featured 骨架卡，数据失败时仍保留可用页面。
- 新增按语言静态 Featured JSON，并保留原全语言接口兼容旧客户端。
- 定时生成 `236x338` WebP 封面缩略图；单张失败时回退原始 HTTPS 封面。
- 真实浏览器冷/热启动、中文/繁中/RTL/英文回退和完整跳转回归。

### 不包含

- 不修改四字符 code 的分配、唯一性、回收和数据库结构。
- 不修改 `{code}` 发布宏、冻结文案、GPU/TikTok 发布流程。
- 不修改 `/api/public/tt-code/resolve` 的响应和限流逻辑。
- 不修改最终 `dramawavew2a.com/ads/101/2250/view` 的参数顺序或字段语义。
- 不引入 Service Worker，也不把完整原始封面存入数据库。

## 用户故事 / 业务规则

- 中文浏览器打开页面时，首屏直接显示中文；英文浏览器直接显示英文。
- 支持语言与现有 COPY 集一致：`en/es/pt/tr/fr/ar/id/vi/th/ja/ko/de/it/ru/hi/tl/ms/pl/cs/el/zh-Hans/zh-Hant`。
- `zh-TW/zh-HK/zh-MO/zh-Hant` 使用繁中，其余 `zh` 使用简中界面；榜单沿用现有 `zh-tw` 数据桶。
- 不支持的首选语言使用英文首屏；有效服务端 locale 不由延后 JS 二次切换。
- 每个语言榜单保持五条、上海昨日消耗排序语义和最后一次成功结果。
- Featured 或缩略图失败不能禁用搜索，也不能生成未经验证的跳转 URL。

## 交互与流程

1. Nginx 从首个 `Accept-Language` 标签选取白名单语言 HTML。
2. HTML 首屏带正确语言文案和五张骨架卡，同时请求带内容哈希的主 JS。
3. 主 JS 复用服务端已验证 locale，不重写同语言首屏；只有标记缺失/非法时
   才读取 `navigator.languages` 兜底。
4. 页面请求 `/api/public/tt-drama/featured-by-language/<language>.json`。
5. 成功后替换五张骨架卡；封面优先使用同源 WebP，失败则保留占位图。
6. 搜索和 Featured 点击仍调用原 resolver，验证目标后才导航。

## 技术设计

### 影响模块

- `static/tt-drama-code-search.html`、`static/tt-drama-code-search.js`
- 新增静态资源构建脚本和生成的 locale HTML/hash JS
- 新增独立的 `features/tt_drama_featured_assets/` 与
  `scripts/refresh_tt_drama_featured_assets.py`，只读取现有 v2 LKG
- `deploy/nginx/tt-drama-search.conf`、`deploy/nginx/tt-drama-code-search.conf`
- 新增独立的 `deploy/tt-drama-featured-assets.service/.path`，原
  `tt-drama-featured.service/.timer` 不变
- TT Featured、Node 合同和真实浏览器测试

### 数据结构

新增按语言只读快照（schema v3）：

```json
{
  "schema_version": 3,
  "source_date": "2026-08-05",
  "generated_at": "2026-08-06T18:00:00+08:00",
  "language": "en",
  "items": [
    {
      "content_id": "xxxxxxxxxx",
      "title": "Story",
      "cover_url": "https://static-v1.mydramawave.com/original-cover.jpg",
      "thumbnail_url": "/tt-featured-covers/<sha256>.webp",
      "language": "en",
      "episode_count": 0
    }
  ]
}
```

- 每个文件严格五条，禁止 spend 字段。
- 文件名只能是经规范化的语言标签；封面文件名为内容 SHA-256。
- 全语言 v2 文件继续保留，避免破坏旧客户端和回滚。

### API / 接口

- 新增：`GET /api/public/tt-drama/featured-by-language/<lang>.json`
- 保留：`GET /api/public/tt-drama/featured-by-language`
- 保留：`GET /api/public/tt-code/resolve?query=...&source=Search|Featured`
- 新增同源静态封面：`GET /tt-featured-covers/<sha256>.webp`

### 异常与边界

- 语言路径和 Nginx alias 仅允许固定白名单/严格正则，禁止路径穿越。
- locale 文件写入采用逐文件原子替换；失败时保留旧文件。
- 缩略图下载限制 HTTPS 主机、超时、最大字节和最大像素；失败只回退原图。
- HTML 保持 `no-store`；只有内容哈希 JS/WebP 使用 immutable。
- Featured 超时调整为 4 秒，但绝不阻塞语言首屏和搜索绑定。
- `/tt/`、`/tt-code/` 继续 404，不新增重定向。

## 验收标准

1. 中文、繁中、阿拉伯语和英文请求的 HTML 响应已是对应语言，且带正确 `Content-Language`。
2. 真实浏览器首次绘制前后不存在英文到目标语言的可见二次切换。
3. 主 JS 使用内容哈希 URL，响应 `public, max-age=31536000, immutable` 且 gzip 生效。
4. HTML、主 JS、locale JSON 的压缩响应均带 `Content-Encoding: gzip`。
5. 页面只请求一个当前语言 JSON；响应五条且不含 spend。
6. 五张骨架卡在主 JS 执行前已存在；搜索/跳转不等待封面。
7. 可生成和公开读取 WebP 缩略图；生成失败时原封面回退仍可用。
8. `/tt` 与 `/tt-code` 功能一致，尾斜杠仍 404。
9. code 搜索、Content ID 搜索、Featured 单击、拖动抑制、完整 W2A 参数全部回归通过。
10. `{code}` 现有发布测试保持通过，未修改生产发布数据。

## 风险与待确认

- 首次访问仍受用户到源站 RTT 影响；本需求消除额外语言等待并减少传输量，不承诺固定公网毫秒值。
- Content-Language 来自请求头的白名单映射；请求头与 `navigator.languages`
  不一致时继续使用已完成首屏的服务端语言，避免延迟二次闪屏。
- WebP 生成依赖生产 Python Pillow 的 WebP 支持；部署前必须实测，失败则不发布缩略图 URL。
- 内容哈希缩略图暂不自动删除；先用内容去重和数据盘容量监控控制风险。

## 变更记录

- 2026-08-06：根据用户录屏和生产冷启动测量建立需求，用户确认执行。
