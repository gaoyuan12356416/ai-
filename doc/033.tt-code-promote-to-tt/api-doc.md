# 路由与宏合同

## 页面入口

### `GET /tt`

- 主入口。
- 返回 `tt-drama-code-search.html`，不重定向。
- 加载 `/tt-drama-code-search.js`。
- 支持四字符 code、剧 ID、多语言 UI 和按语言 Featured Top 5。

### `GET /tt-code`

- 兼容入口。
- 与 `/tt` 返回同一静态 HTML，行为和数据接口完全一致。

## 页面使用的接口

- `GET /api/public/tt-code/resolve?query=<value>&source=Search|Featured`
- `GET /api/public/tt-drama/featured-by-language`

旧接口 `/api/public/tt-drama/resolve` 与 `/api/public/tt-drama/featured` 继续保留，仅供旧静态回滚链路使用。

## `{code}` 宏

- 合法形式：精确 `{code}`。
- 分配时机：正式自动/排期 queue 创建事务。
- 输出格式：`A-Z` 与 `0-9` 组成的四字符唯一 code。
- caption 合同：queue 落库时宏已替换；GPU/TikTok payload 不得出现字面量 `{code}`。
- 立即测试：返回 HTTP 409，错误码 `tt_post_code_macro_queue_only`。
- 幂等重试：相同正式 queue 复用已经冻结的 code 和 caption，不重新分配。

## 兼容说明

新 `/tt` 不再执行旧页面的入口 query 参数透传。最终 W2A 归因参数来自 code 对应的发布记录、剧 ID 最新发布记录或通用 fallback，并按搜索来源将 `af_channel` 设为 `TT`、`Search` 或 `Featured`。
