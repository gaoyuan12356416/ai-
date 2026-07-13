# Meta/Facebook 试玩广告生成接口

更新时间：2026-07-10

当前产物格式：Meta 单文件试玩广告（`index.html`）

## 1. 接口说明

该接口接收可运行的静态 HTML 或 ZIP 游戏包，生成符合 Meta/Facebook 试玩广告要求的单文件 HTML 和 ZIP。

新版生成逻辑会：

- 把游戏入口及本地脚本、样式、图片、音频、WASM、JSON 和归档资源嵌入一个 `index.html`；
- 使用 `LZMA + script-safe Base91` 压缩大型运行时资源；
- 在执行原游戏脚本前完成内存资源包初始化；
- 用内嵌资源读取层替换游戏中的原生 `XMLHttpRequest` 和 `fetch` 资源加载；
- 拦截 Defold `_dmSysOpenURL`、包装层安装按钮等安装动作，并只调用 `FbPlayableAd.onCTAClick()`；
- 拒绝残留的外部资源、原生网络请求、路径穿越和直接页面跳转；
- 同时校验最终 HTML 与 ZIP 的实际字节数。

接口只负责生成试玩广告资产，不会自动上传 Meta 或创建广告。

## 2. 接口地址

推荐地址：

```http
POST https://ai.yingliangads.com/api/fb-playable/preview
```

兼容旧地址：

```http
POST https://ai.yingliangads.com/api/ad-material/playable-preview
```

两个地址调用同一套生成逻辑。

## 3. 鉴权

当服务端配置了 `PLAYABLE_PREVIEW_API_TOKEN` 或兼容变量 `FB_PLAYABLE_API_TOKEN` 时，请任选一种方式传递令牌：

```http
Authorization: Bearer <token>
```

或：

```http
X-API-Token: <token>
```

未配置令牌时不要求该请求头。不要把令牌写入游戏 HTML、公开日志或客户端代码。

## 4. 请求格式

### 4.1 multipart/form-data 上传

推荐用于上传本地 `.html` 或 `.zip` 文件。

文件字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `static_page` | 是 | 推荐字段，上传 HTML 或完整 ZIP 静态包 |
| `file` | 否 | 兼容旧字段 |
| `static_file` | 否 | 兼容旧字段 |
| `game_file` | 否 | 兼容旧字段 |
| `zip` | 否 | 兼容旧字段 |
| `html` | 否 | 兼容旧字段 |

文件字段只需传一个，优先使用 `static_page`。

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

HTTP 状态码为 `200`，示例：

```json
{
  "preview_id": "937e8724054e431183b61465a04dbf06",
  "preview_html_url": "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/ad-materials/playable-preview/937e8724054e431183b61465a04dbf06/index.html",
  "zip_url": "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/ad-materials/playable-preview/937e8724054e431183b61465a04dbf06/playable-preview.zip",
  "manifest_url": "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/ad-materials/playable-preview/937e8724054e431183b61465a04dbf06/manifest.json",
  "documentation_url": "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/ad-materials/docs/playable-preview-api.md",
  "trial_seconds": 20,
  "play_count": 1,
  "store_url": "https://play.google.com/store/apps/details?id=ai.fream.dramawave",
  "entry": "index.html",
  "source_entry": "game/index.html",
  "html_size": 4785932,
  "zip_size": 3920963,
  "meta_size_limit_bytes": 4800000,
  "size_headroom_bytes": 14068,
  "meta_compatible": true,
  "compatibility": {
    "single_file": true,
    "native_network_requests": 0,
    "direct_redirects": 0,
    "unsafe_eval_calls": 0,
    "csp_safe_script_bootstrap": true,
    "cta_hook": "FbPlayableAd.onCTAClick",
    "resource_encoding": "lzma+base91",
    "embedded_file_count": 17,
    "html_size": 4785932,
    "zip_size": 3920963,
    "meta_size_limit_bytes": 4800000
  },
  "languages": ["en", "zh", "zh-cn"]
}
```

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
- HTML 中没有外部资源 URL、原生 `XMLHttpRequest`/`fetch`、`window.open` 或 location 直接跳转；
- 游戏脚本通过真实 `<script>` 节点顺序启动，不使用 Meta 沙箱禁止的 `eval` / `unsafe-eval`；
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
| `missing upload file` | multipart 请求没有文件字段 |
| `missing static_html...` | JSON 未提供 HTML 或 ZIP 内容字段 |
| `missing store_url` | 未提供商店链接 |
| `store_url must start with http:// or https://` | 商店链接格式不合法 |
| `uploaded static page must include an html entry` | ZIP 中未找到 HTML 入口 |
| `unsafe zip entry` | ZIP 存在绝对路径或父目录穿越 |
| `generated Meta playable HTML exceeds safety limit` | 最终 HTML 超过安全上限 |
| `generated Meta playable zip exceeds limit` | 最终 ZIP 超过安全上限 |
| `external markup URL remains` | HTML 仍引用外部资源 |
| `Meta compatibility validation failed` | 仍有原生网络调用、直接跳转、unsafe-eval 启动器或缺少 CTA Hook |

鉴权失败返回 HTTP `403`：

```json
{
  "error": "forbidden"
}
```

## 11. 调用方验收建议

生成成功后，调用方仍应在上传 Meta 前完成以下检查：

1. 下载 `preview_html_url` 和 `zip_url`，按实际下载字节重新测量体积；
2. 解压 ZIP，确认文件列表严格为 `index.html`；
3. 扫描 HTML，确认没有直接商店跳转和外部资源；
4. 在禁止 `unsafe-eval` 的 CSP 浏览器沙箱中注入 `FbPlayableAd.onCTAClick` 测试桩，确认无 CSP 报错且游戏进入可交互场景；
5. 最后在 Meta Ads Manager 预览环境中完成平台侧验证。
