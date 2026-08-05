# 033.tt-code-promote-to-tt 需求与技术设计

## 背景

多语言四字符搜索页已经在 `/tt-code` 独立运行，但 TikTok 账号历史 Bio 链接仍指向 `/tt`。本次将 `/tt-code` 的页面能力提升为 `/tt` 主入口，使原链接直接生效，同时保留 `/tt-code` 兼容入口。

## 目标

1. `/tt` 与 `/tt-code` 加载同一份多语言、四字符 code/剧 ID 搜索页面。
2. `/tt` 保持 exact path、HTTP 200、无重定向和 `no-store`，已有账号无需修改链接。
3. 保留旧 HTML、JS、v1 resolver 和 v1 Featured 路由，形成单配置文件快速回滚能力。
4. 再次验证精确宏 `{code}` 在正式自动/排期队列冻结时生成唯一四字符 code，并把最终文案原样作为 GPU/TikTok `title`。

## 范围

### 包含

- 修改 `/tt` 的 Nginx exact location，使其 alias 到 `tt-drama-code-search.html`。
- `/tt` 同步 `/tt-code` 的缓存、安全响应头和 GET-only 约束。
- 保留 `/tt-code` 为同页兼容入口，不重定向。
- 更新 Node、Python 和真实浏览器回归，使主流程从 `/tt` 执行。
- 增加正式自动排期 `{code}` 冻结至 GPU 请求文案的闭环回归，以及直接测试拒绝边界回归。

### 不包含

- 不修改 `app.py`、code resolver、Redis、Featured 刷新服务或榜单数据。
- 不切换 `/opt/tt-post/current`，不重启 TT 发布服务，不触发真实 TikTok 发布。
- 不覆盖旧 `tt-drama-search.html/js`；线上旧 JS 存在独立 W2A 路径差异，必须作为原样回滚资产保留。
- 不把自动配置的新描述模板回写到已经冻结的历史素材池记录。

## 业务规则

- `/tt` 是主入口，`/tt-code` 是兼容入口；两者必须返回同一 HTML 内容并加载同一新 JS。
- 页面搜索四字符 code 时保留发布记录中的 `af_channel=TT`；直接搜索剧 ID 和 Featured 分别使用 `Search` 与 `Featured`。
- `/tt` 入口 query string 可以保留在浏览器地址栏，但新页面的最终归因参数以后台冻结发布记录或通用 fallback 为准，不透传旧入口参数。
- `{code}` 只识别精确小写单花括号形式；正式 queue 在同一数据库事务中先分配 code/route，再冻结最终 caption。
- “立即测试”不具备正式 durable queue identity，含 `{code}` 时必须返回 409 `tt_post_code_macro_queue_only`。
- 已进入素材池的记录继续使用各自冻结的 `caption_template`；用户保存含 `{code}` 的新模板后，新入池素材和由其生成的正式队列生效。

## 技术设计

### 路由

- `deploy/nginx/tt-drama-search.conf` 中唯一的 `location = /tt` 改为：
  - alias `/usr/share/nginx/html/tt-drama-code-search.html`
  - `Cache-Control: no-store`
  - `Pragma: no-cache`
  - `X-Frame-Options: DENY`
  - 与新页一致的 CSP
  - `limit_except GET`
- `deploy/nginx/tt-drama-code-search.conf` 继续声明 `/tt-code`、新 JS、分语言榜单与 code resolver；禁止在这里重复声明 `/tt`。

### 发布宏闭环

1. 素材入池时保存含 `{code}` 的模板，但不分配 code。
2. 正式自动/排期 run 创建 queue 时，在单一 SQLite 事务中分配唯一 `[A-Z0-9]{4}` code 和 route。
3. 使用该 code 单次渲染 queue caption，落库后不得残留 `{code}`。
4. 发布时 `GPUClient.publish` 将冻结的 `queue.caption` 原样写入请求 `title`；GPU worker 再原样放入 TikTok `post_info.title`。

## 验收标准

- `/tt` 和 `/tt-code` 均为 200、无重定向，HTML 字节一致，均只加载新 JS。
- `/tt` 的标题、语言自适应、每语言 5 条 Featured、拖动与单击行为全部与已验收的新页一致。
- `/tt/` 不被误匹配为 exact location。
- Nginx 配置仅声明一次 exact `/tt`，`nginx -t` 成功。
- 旧 HTML/JS 与旧 API 路由未被覆盖或删除。
- 自动排期测试生成唯一四字符 code，caption 无宏残留，Fake GPU 收到的 caption 与冻结值完全一致。
- GPUClient HTTP payload 的 `title` 与传入 caption 完全一致。
- 生产部署来自 GitHub 精确提交；先备份单个 Nginx 配置，失败可一条路径恢复。

## 变更记录

- 2026-08-05：创建 033 需求，确认 `/tt` 替换为新页、`/tt-code` 保留兼容，并复核 `{code}` 正式发布链路。
