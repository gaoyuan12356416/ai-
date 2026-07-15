# Meta/Facebook 试玩广告生成接口

更新时间：2026-07-15

当前产物格式：Meta 单文件试玩广告（`index.html`）

## 1. 接口说明

该接口接收可运行的静态 HTML 或 ZIP 游戏包，生成符合 Meta/Facebook 试玩广告要求的单文件 HTML 和 ZIP。

新版生成逻辑会：

- 把游戏入口及本地脚本、样式、图片、音频、WASM、JSON 和归档资源嵌入一个 `index.html`；
- 使用 `LZMA + script-safe Base94` 压缩大型运行时资源；
- 在执行原游戏脚本前完成内存资源包初始化；
- 在游戏脚本启动前把 iframe 内的原生 `fetch` / `XMLHttpRequest` 运行时绑定到内存资源层，由该层接管资源加载；
- 把可执行脚本中的直接 `location` 引用改写到只读导航门面，锁定 `window.open`，并在捕获阶段阻止动态 `<a>` / `<area>` 外链导航；
- 将 `setTimeout` / `setInterval` 锁定为只接受函数回调的安全包装，拒绝 `eval`、`Function` 及其常见别名/构造器绕过；
- 使用不含 `allow-same-origin` 的 iframe 沙箱，并在外层与游戏内层同时嵌入禁止网络、Worker、Object 和表单提交的 CSP；
- 拦截 Defold `_dmSysOpenURL`、包装层安装按钮等安装动作，并只调用 `FbPlayableAd.onCTAClick()`；
- 拒绝残留的外部资源、外网能力、路径穿越和直接页面跳转；
- 同时校验最终 HTML 与 ZIP 的实际字节数。

接口只负责生成试玩广告资产，不会自动上传 Meta 或创建广告。

该接口用于处理团队可控、来源可信的游戏包。静态扫描、运行时门面、sandbox 与 CSP 是多层防护，但不应把它当成可安全执行任意敌对 JavaScript 的通用隔离服务。

## 2. 接口地址

唯一对外接口地址：

```http
POST https://ai.yingliangads.com/api/fb-playable/preview
```

## 3. 鉴权

生产环境强制鉴权。每次请求都必须携带服务端分配的 `PLAYABLE_PREVIEW_API_TOKEN`，请任选一种方式传递令牌：

```http
Authorization: Bearer <token>
```

或：

```http
X-API-Token: <token>
```

不要把令牌写入游戏 HTML、公开日志或浏览器客户端代码。服务端支持 `off`、`observe`、`enforce` 三种迁移模式，但生产配置固定使用 `enforce`；兼容环境变量 `FB_PLAYABLE_API_TOKEN` 仅用于旧部署迁移。

## 4. 请求格式

### 4.1 multipart/form-data 上传

推荐用于上传本地 `.html` 或 `.zip` 文件。

文件字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `static_page` | 六选一 | 推荐字段，上传 HTML 或完整 ZIP 静态包 |
| `file` | 六选一 | 兼容旧字段 |
| `static_file` | 六选一 | 兼容旧字段 |
| `game_file` | 六选一 | 兼容旧字段 |
| `zip` | 六选一 | 兼容旧字段 |
| `html` | 六选一 | 兼容旧字段 |

文件字段只能传一个，优先使用 `static_page`；重复文本字段或同时上传多个文件会被拒绝。

文本字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `store_url` | string | 是 | `http://` 或 `https://` 商店链接；仅保留给后续投放配置，不会写成 HTML 跳转 |
| `play_count` | int | 否 | 最大试玩次数，默认及有效最小值为 `1` |
| `title` | string | 否 | 生成 HTML 的标题 |
| `trial_seconds` | int | 否 | 每次试玩时长，默认 `20` 秒，限制为 `1..120` |
| `headline_text` | string | 否 | 覆盖英文结束标题 |
| `subtitle_text` | string | 否 | 覆盖英文结束说明 |
| `cta_text` | string | 否 | 覆盖英文 CTA 文案 |
| `install_text` | string | 否 | 兼容旧字段，等同于 `cta_text` |
| `play_label` | string | 否 | 覆盖英文试玩次数标签 |
| `translations` | JSON string | 否 | 覆盖或新增多语言文案 |

示例：

```bash
curl -X POST 'https://ai.yingliangads.com/api/fb-playable/preview' \
  -H 'Authorization: Bearer <token>' \
  -F 'static_page=@game.zip;filename=game.zip' \
  -F 'store_url=https://play.google.com/store/apps/details?id=ai.fream.dramawave' \
  -F 'play_count=1' \
  -F 'trial_seconds=20' \
  -F 'title=Boxrob'
```

### 4.2 application/json 上传

适合服务间调用。内容字段任选一个：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `static_html` | string | 直接传 HTML 文本 |
| `static_html_base64` | string | 传 HTML 文件的 Base64 |
| `static_zip_base64` | string | 传 ZIP 文件的 Base64 |
| `html` | string | `static_html` 的兼容旧字段 |
| `html_base64` | string | `static_html_base64` 的兼容旧字段 |
| `zip_base64` | string | `static_zip_base64` 的兼容旧字段 |
| `filename` | string | 可选；用于识别上传内容是 `.html` 还是 `.zip`，未提供时按所选内容字段自动补齐 |

其他可用字段与 multipart 文本字段一致；`play_times`、`plays` 也可作为 `play_count` 的兼容别名。

示例：

```json
{
  "static_html": "<!doctype html><html><body>...</body></html>",
  "filename": "index.html",
  "store_url": "https://play.google.com/store/apps/details?id=ai.fream.dramawave",
  "play_count": 1,
  "trial_seconds": 20,
  "title": "Playable Preview"
}
```

接口不接受 `static_page_url` 或 `game_url`。调用方必须先下载完整静态包，再上传文件内容；仅提供一个远程入口 URL 无法保证依赖资源完整，也不符合 Meta 单文件要求。

## 5. 成功返回

HTTP 状态码为 `200`。以下地址中的 `<preview_id>` 由每次成功请求动态生成，示例数值仅用于说明字段格式：

```json
{
  "preview_id": "<preview_id>",
  "preview_html_url": "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/ad-materials/playable-preview/<preview_id>/index.html",
  "zip_url": "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/ad-materials/playable-preview/<preview_id>/playable-preview.zip",
  "manifest_url": "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/ad-materials/playable-preview/<preview_id>/manifest.json",
  "documentation_url": "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/ad-materials/docs/playable-preview-api.md",
  "trial_seconds": 20,
  "play_count": 1,
  "store_url": "https://play.google.com/store/apps/details?id=ai.fream.dramawave",
  "entry": "index.html",
  "source_entry": "game/index.html",
  "html_size": 4782231,
  "zip_size": 3933763,
  "meta_size_limit_bytes": 4800000,
  "size_headroom_bytes": 17769,
  "meta_compatible": true,
  "compatibility": {
    "single_file": true,
    "native_network_requests": 0,
    "direct_redirects": 0,
    "unsafe_eval_calls": 0,
    "csp_safe_script_bootstrap": true,
    "navigation_guard": true,
    "safe_timer_wrappers": true,
    "embedded_csp": true,
    "opaque_origin_sandbox": true,
    "cta_hook": "FbPlayableAd.onCTAClick",
    "resource_encoding": "lzma+base94",
    "embedded_file_count": 18,
    "html_size": 4782231,
    "zip_size": 3933763,
    "meta_size_limit_bytes": 4800000
  },
  "languages": ["ar", "de", "en", "es", "fr", "hi", "id", "it", "ja", "ko", "ms", "pt", "ru", "th", "tr", "vi", "zh", "zh-cn", "zh-tw"]
}
```

上例中的 `compatibility` 仅展示常用字段。实际对象还会返回压缩包文件数、原始/压缩/编码字节数、内联文件数、原始入口、HTML 余量和最终双产物余量等审计字段。

关键字段：

| 字段 | 说明 |
| --- | --- |
| `preview_html_url` | 公网 HTML 预览地址 |
| `zip_url` | 可上传 Meta Ads Manager 的 ZIP 地址 |
| `manifest_url` | 本次生成的审计与兼容信息 |
| `documentation_url` | 当前接口文档地址 |
| `entry` | 最终产物入口，固定为 `index.html` |
| `source_entry` | 上传包中识别到的原始入口 |
| `html_size` | 最终 UTF-8 HTML 的实际字节数 |
| `zip_size` | 最终 ZIP 的实际字节数 |
| `meta_size_limit_bytes` | 当前服务采用的安全上限 |
| `size_headroom_bytes` | HTML 与 ZIP 两项中较小的剩余空间 |
| `meta_compatible` | 只有最终 HTML 与 ZIP 均通过校验时才为 `true` |
| `compatibility` | 单文件、网络请求、跳转、CSP 安全启动、CTA 和资源压缩等校验结果 |

`manifest_url` 对应的 JSON 与接口成功返回使用同一套审计口径。manifest 顶层固定包含：`preview_id`、`title`、`trial_seconds`、`play_count`、`store_url`、`documentation_url`、`source_entry`、`entry`、`html_size`、`zip_size`、`meta_size_limit_bytes`、`size_headroom_bytes`、`meta_compatible`、`compatibility` 和 `languages`。

## 6. 产物结构

下载 `zip_url` 后，ZIP 中必须且只允许存在一个顶层文件：

```text
index.html
```

以下旧版结构已经废弃，不会再生成：

```text
index.html
game/index.html
game/assets/...
```

原始文件和解压后的 `game/` 目录不会保留在发布产物中；`manifest.json` 用于审计，但不放进 Meta ZIP。

## 7. Meta 兼容和体积规则

Meta 上传页面提示的“最大 5 MB”不能按 `5 MiB` 或只检查压缩 ZIP 来处理。当前接口采用更保守的十进制安全上限：

```text
4,800,000 bytes
```

必须同时满足：

- 最终 UTF-8 `index.html <= 4,800,000 bytes`；
- 最终 `playable-preview.zip <= 4,800,000 bytes`；
- ZIP 只包含顶层 `index.html`；
- HTML 中没有可直连外网的资源 URL、`window.open` 或 location 直接跳转；原游戏代码里的 `XMLHttpRequest`/`fetch` 会在运行时被绑定到内存资源层；
- 直接 `location` 引用会改写到不可导航的门面，动态链接点击和 popup 会被 CTA 桥接层接管；
- 游戏脚本通过真实 `<script>` 节点顺序启动，不使用 Meta 沙箱禁止的 `eval` / `unsafe-eval`；
- iframe 使用 opaque-origin sandbox，内外 CSP 都设置 `connect-src 'none'`、`worker-src 'none'`、`object-src 'none'` 和 `form-action 'none'`；
- CTA 只调用 `FbPlayableAd.onCTAClick()`。

因此，即使 ZIP 小于 5 MB，只要解压后的 HTML 超过安全上限，接口仍会拒绝生成。

## 8. CTA 与商店链接

`store_url` 是必填业务字段，会返回给下游投放流程并写入 manifest，但不会成为最终 HTML 的 `href`、`window.open` 或 location 跳转。

试玩内所有安装动作都统一调用：

```javascript
FbPlayableAd.onCTAClick()
```

Meta Ads Manager 会结合广告层配置处理实际安装跳转。在普通浏览器中单独打开预览页时，如果没有注入 `FbPlayableAd`，点击 CTA 不会直接打开商店，这是预期行为。

## 9. 多语言文案

默认支持：

```text
ar, de, en, es, fr, hi, id, it, ja, ko, ms, pt, ru, th, tr, vi, zh, zh-cn, zh-tw
```

匹配顺序：

1. 完整浏览器语言标签，例如 `zh-tw`；
2. 基础语言，例如 `pt-br` 回退到 `pt`；
3. 英文 `en`。

覆盖示例：

```bash
curl -X POST 'https://ai.yingliangads.com/api/fb-playable/preview' \
  -H 'Authorization: Bearer <token>' \
  -F 'static_page=@game.zip;filename=game.zip' \
  -F 'store_url=https://play.google.com/store/apps/details?id=ai.fream.dramawave' \
  -F 'translations={"en":{"headline":"Demo Finished","subtitle":"Install to continue.","cta":"Install to Play More","plays":"Plays"},"zh-cn":{"headline":"试玩结束","subtitle":"安装后继续游戏","cta":"安装后继续玩","plays":"试玩次数"}}'
```

每个语言对象支持 `headline`、`subtitle`、`cta`、`plays`；兼容旧键 `title`、`description`、`button`、`install`。

## 10. 错误返回

失败时通常返回 HTTP `400`：

```json
{
  "code": "bad_request",
  "error": "missing store_url",
  "message": "missing store_url"
}
```

常见错误：

| 错误内容 | 原因 |
| --- | --- |
| `empty request body` | 请求体为空 |
| `upload too large` | 原始上传内容超过服务端上传限制，默认 80 MiB |
| `duplicate multipart field` | multipart 中同一个文本字段出现多次 |
| `multiple upload files are not supported` | 同时传入多个文件或多个文件别名 |
| `missing upload file` | multipart 请求没有文件字段 |
| `missing static_html...` | JSON 未提供 HTML 或 ZIP 内容字段 |
| `Only base64 data is allowed` | JSON Base64 内容包含非法字符或格式不完整 |
| `missing store_url` | 未提供商店链接 |
| `store_url must start with http:// or https://` | 商店链接格式不合法 |
| `uploaded static page must include an html entry` | ZIP 中未找到 HTML 入口 |
| `unsafe zip entry` | ZIP 存在绝对路径、盘符路径或父目录穿越 |
| `zip extracted size exceeds limit` | ZIP 声明或实际解压体积超过限制，默认 80 MiB |
| `zip contains too many files` | ZIP 目录项总数（文件和目录）超过限制，默认 4096 |
| `duplicate zip entry` | ZIP 中存在重复目标路径 |
| `encrypted zip entries are not supported` | ZIP 含加密文件 |
| `zip symbolic links are not supported` | ZIP 含符号链接 |
| `zip special entries are not supported` | ZIP 含设备、FIFO、socket 等普通文件/目录以外的特殊条目 |
| `zip file and directory paths conflict` | ZIP 把同一路径同时作为文件和目录祖先使用 |
| `unsupported zip compression method` | ZIP 不是 Stored 或 Deflate 压缩方法 |
| `generated Meta playable HTML exceeds safety limit` | 最终 HTML 超过安全上限 |
| `generated Meta playable zip exceeds limit` | 最终 ZIP 超过安全上限 |
| `external markup URL remains` | HTML 仍引用外部资源 |
| `Meta compatibility validation failed` | 仍有外部资源、直接跳转、unsafe-eval 启动器、受禁浏览器能力或缺少 CTA Hook |
| `invalid multipart request body` | multipart Content-Type、边界或正文不完整 |
| `invalid multipart field encoding` | multipart 文本字段声明了未知或不可解码字符集 |
| `JSON request body must be an object` | JSON 顶层不是对象 |
| `uploaded content is empty` | 文件或 Base64 解码后的内容为空 |
| `play_count must be >= 0` | 试玩次数传入负数 |
| `File is not a zip file` | 上传内容不是有效 ZIP |
| `CSS @import is not supported` | CSS 仍依赖无法安全打包的导入样式 |
| `CSS image-set is not supported` | CSS 使用无法可靠逐项内联校验的 `image-set` |
| `Base94 package round-trip failed` | 内嵌压缩资源包自校验失败 |

鉴权失败返回 HTTP `403`：

```json
{
  "code": "forbidden",
  "error": "forbidden",
  "message": "forbidden"
}
```

如果生产配置为 `enforce` 但服务端令牌缺失，接口会返回 HTTP `503` 和 `auth_not_configured`，避免意外降级为匿名访问。服务端只记录鉴权模式、是否携带令牌、校验结果和放行/拒绝决定，不记录令牌原文。

非预期服务端故障返回 HTTP `500` 和通用 `internal_error`，详细异常只保留在服务端日志中。

为限制大文件解析与压缩的内存峰值，生产默认同一时间只处理一个生成请求。并发超出 `PLAYABLE_PREVIEW_MAX_CONCURRENCY` 时返回 HTTP `429` 和 `playable_preview_busy`，调用方应稍后重试。

## 11. 调用方验收建议

生成成功后，调用方仍应在上传 Meta 前完成以下检查：

1. 下载 `preview_html_url` 和 `zip_url`，按实际下载字节重新测量体积；
2. 解压 ZIP，确认文件列表严格为 `index.html`；
3. 扫描 HTML，确认没有直接商店跳转和外部资源；
4. 在禁止 `unsafe-eval` 的 CSP 浏览器沙箱中注入 `FbPlayableAd.onCTAClick` 测试桩，确认无 CSP 报错且游戏进入可交互场景；
5. 最后在 Meta Ads Manager 预览环境中完成平台侧验证。
