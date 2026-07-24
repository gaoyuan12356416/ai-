# 014.tt-content-id-bridge-passthrough 需求与技术设计

## 背景

TikTok 主页只能稳定维护一个链接。现有移动端中间页支持用户输入 DramaWave `content_id` 后跳转到固定 W2A 落地页，但页面暂时托管在 COS，且不能把入口链接上的投放追踪参数带到最终落地页。

## 目标

- 将公开入口迁移到短路径 `https://ai.yingliangads.com/tt`。
- 用户输入 `content_id` 后，固定生成 DramaWave W2A 链接。
- 将入口 URL 中的附加查询参数安全透传到 W2A 链接。
- 防止入口参数覆盖 `af_dp`、`c`、`af_c_id` 三个核心参数。

## 范围

### 包含

- 独立移动端搜索页及前端参数拼接逻辑。
- 附加参数校验、数量/长度边界和重复参数保留。
- Node 契约测试及移动端真实浏览器验证。
- AI 后台源码、生产静态目录和回滚备份。

### 不包含

- 不查询或同步完整剧库。
- 不判断 `content_id` 是否真实存在；W2A 负责解析。
- 不修改 TikTok 主页、广告或 DramaWave W2A 服务。
- 不新增数据库、后端 API、登录权限或重定向目标配置。

## 用户故事 / 业务规则

1. 用户从带追踪参数的 TikTok 主页链接进入中间页。
2. 用户输入视频结尾展示的 DramaWave `content_id`。
3. 页面生成固定目标：`https://www.dramawavew2a.com/ads/0/2049/view`。
4. `af_dp` 取用户输入值，`c` 固定为 `TTpost`，`af_c_id` 固定为 `0001`。
5. 入口 URL 中其余合法参数按原顺序追加；同名重复参数保留。
6. 入口 URL 中大小写任意的核心参数或页面控制参数均不得透传。
7. 页面只显示透传参数数量，不回显参数值。

## 交互与流程

入口 URL -> 读取并过滤附加参数 -> 输入 `content_id` -> 本地校验 -> 生成目标链接 -> 用户点击进入 W2A。

## 技术设计

### 影响模块

- `static/tt-drama-search.html`
- `static/tt-drama-search.js`
- `scripts/test_tt_drama_bridge.js`
- `deploy/nginx/tt-drama-search.conf`

### 数据结构

无持久化数据。附加参数以有序 `[key, value]` 列表在浏览器内短暂处理。

### API / 接口

无新增后端 API。页面只生成固定域名、固定路径的 HTTPS 链接。

### 异常与边界

- `content_id` 仅允许 `A-Z a-z 0-9 _ -`，长度 `10..32`。
- 最多透传 40 个参数；键最长 100 字符，值最长 1024 字符。
- 参数键仅允许字母开头，后续为字母、数字、点、下划线或连字符。
- 超出边界或非法参数忽略，并在结果文案中提示有参数被忽略。
- 保留参数值的 URL 语义；最终由 `URLSearchParams` 统一编码。
- 目标主机与路径不可由入口参数改变，避免开放重定向。
- Nginx 精确匹配 `/tt` 并直接返回页面，不发生末尾斜杠跳转，入口查询参数保留在浏览器地址栏。

## 验收标准

- `?af_adset_id=XXX` + `l9rP6ey2CB` 精确生成：
  `https://www.dramawavew2a.com/ads/0/2049/view?af_dp=l9rP6ey2CB&c=TTpost&af_c_id=0001&af_adset_id=XXX`
- 多个附加参数和重复参数按顺序保留。
- 外部传入的 `af_dp`、`c`、`af_c_id` 无法覆盖固定值。
- 公开 URL 无需登录，可在 390×844 移动端视口正常使用。
- 代码已提交并推送，生产文件有可验证备份和明确回滚命令。

## 风险与待确认

- W2A 对不存在的 `content_id` 可能仍返回 HTTP 200 或默认剧，因此本页不把 HTTP 状态当作剧集有效性证明。
- 任意合法附加参数会传给 W2A；调用方需避免在 URL 中放入个人敏感信息。

## 变更记录

- 2026-07-24：创建需求，确认固定核心参数、附加参数透传及 `/tt` 公开短路径方案。
