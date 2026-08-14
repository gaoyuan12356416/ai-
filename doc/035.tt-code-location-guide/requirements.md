# 035.tt-code-location-guide 需求与技术设计

## 背景

TikTok 用户进入 `/tt` 后需要输入视频文案中的 4 位 code，但当前页面没有说明 code 在哪里。用户提供了一张已对敏感正文和示例 code 做模糊处理的 3:4 截图，希望作为就地引导，同时不能破坏既有搜索、Featured 和 W2A 归因链路。

## 目标

- 在搜索区域内提供可见但克制的 code 位置引导。
- 移动端优先，展开后能清晰查看用户提供的完整示例图。
- 保持 23 个现有 UI locale、搜索成功结果和 Featured 布局自洽。
- 资产同源、轻量、可回滚，不增加第三方依赖。

## 范围

### 包含

- 搜索卡片内默认折叠的原生 `<details>` 引导。
- 常驻 3:4 缩略图、本地化标题和说明；展开后显示完整示例图。
- 用户图转换为 720×960、content-addressed WebP。
- 23 个 locale 的引导文案和图片 alt。
- Nginx 精确 immutable 图片路由、静态构建及浏览器回归。

### 不包含

- 不修改 Resolver、Featured JSON、W2A 目标、八字段归因或数据库。
- 不改 `/tt`、`/tt-code`、`/tt/` 的路径合同。
- 不新增埋点、弹窗库、第三方字体或外链图片。
- 不恢复或猜测示例图内已模糊的 code/正文。

## 用户故事 / 业务规则

1. 用户在不知道 code 位置时，可直接在搜索说明下看到“在哪里找代码”的引导。
2. 用户点击引导后可查看完整示例图；再次点击可收起。
3. 搜索成功后，引导自动收起并隐藏，让匹配结果和 CTA 保持最高优先级。
4. 修改输入导致结果收起时，引导以折叠状态重新出现。
5. 打开引导不得调用 Resolver、触发 Featured 或跳转 W2A。

## 交互与流程

```text
进入 /tt
  -> 搜索卡片 + 折叠引导
  -> 点击引导：原生 details 展开完整图
  -> 输入 code/Content ID 并搜索
      -> 成功：引导收起并隐藏，显示结果 CTA
      -> 失败：引导保留，帮助用户核对 code 位置
```

## 技术设计

### 影响模块

- `static/tt-drama-code-search.html`：引导 DOM 和响应式样式。
- `static/tt-drama-code-search.js`：23 语文案及搜索成功时收起状态。
- `scripts/build_tt_drama_code_assets.js`：静态 copy 合同。
- `static/tt-drama-code-locales/*.html`、hashed JS：生成产物。
- `deploy/nginx/tt-drama-code-search.conf`：WebP 精确缓存路由。
- TT bridge/browser tests：构建、交互和尺寸回归。

### 数据结构

无数据库或缓存结构变化。新增静态资产：

- `tt-code-location-guide.0b42fbc64ab4.webp`
- 720×960，46,114 bytes
- SHA-256：`0b42fbc64ab49e1c58a6f478a8c8f64c90427ce5ef78d06f8b0b145433b2c0`

### API / 接口

无新 API。既有 `/api/public/tt-code/resolve` 和 `/api/public/tt-drama/featured-by-language/{lang}.json` 请求/响应合同保持不变。

### 异常与边界

- 图片加载失败不影响表单、Resolver 或 CTA。
- 无 JS 时原生 `<details>` 仍可展开，locale HTML 已在服务端静态本地化。
- 320px 宽度、390×844、桌面 max-width 520px 及 RTL 均不得横向溢出。
- 缩略图和完整图固定尺寸，避免 CLS；同一 URL 由浏览器缓存复用。
- 引导展开后搜索成功，必须关闭 open 状态，避免结果消失后突然恢复大图。

## 验收标准

- 23 份 locale HTML 的缩略图和完整图均引用同一 hashed WebP，且不保留 i18n build marker。
- 引导默认折叠、可键盘操作、完整图 alt 本地化。
- Search 成功时引导不可见且已收起；失败时仍可用。
- 320/390px、桌面及阿拉伯语无横向溢出或遮挡。
- 有效 4 位 code、Content ID、Featured 跳转和归因行为与改动前一致。
- WebP 响应为 200、`image/webp`、`public, max-age=31536000, immutable`。
- `/tt` HTML 保持 `no-store`；Nginx、API、Redis、TT/Featured 服务不因本需求重启。

## 风险与待确认

- 新缩略图会增加约 46 KB 首次下载；相较原 JPEG 减少约 90.1%，并使用 immutable 缓存。
- 示例图内部为英文，但周边操作文案覆盖 23 个 locale；本需求不生成内容不同的 23 张图片。
- `/tt/` 继续 404 属于既有精确路径合同，不在本需求变更。

## 变更记录

- 2026-08-14：建立需求、交互、静态资产和回滚边界。
