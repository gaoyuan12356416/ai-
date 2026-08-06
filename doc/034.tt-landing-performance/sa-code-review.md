# SA 代码评审

## 结论

通过。两轮独立评审提出的 5 个部署前问题均已修复并复核关闭；无遗留
P0/P1/P2 问题。

## 评审范围

- locale HTML/hash JS 构建、首屏语言与 Featured 前端逻辑。
- Nginx locale map、gzip/cache、JSON/WebP 严格静态路由。
- 独立 Featured 资产生成器、Pillow 固定环境与 systemd sandbox。
- `{code}`、resolver、W2A 跳转和旧 Featured 兼容路径。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P1 | `static/tt-drama-code-search.js` | navigator 二次改写可能重新产生语言闪屏 | 有效服务端 locale 作为权威；仅无效标记时兜底 | 已修复/复核关闭 |
| CR-002 | P2 | locale map | 二字母前缀会误匹配 `arq` 等标签 | 增加语言标签严格边界 | 已修复/复核关闭 |
| CR-003 | P2 | HTML CSS | 新 lead span 被旧 `h1 span` 渐变误覆盖 | 渐变仅作用于 `#page-title-accent` | 已修复/复核关闭 |
| CR-004 | P3 | 静态构建 | Windows CRLF 可导致内容 hash 漂移 | attributes 固定 LF，构建器同时归一化 | 已修复/复核关闭 |
| CR-005 | P2 | Featured assets | Pillow/WebP 全局故障会被误报为单图失败 | 发布前实际 WebP smoke test；全量失败保留 LKG 并非零退出 | 已修复/复核关闭 |

## 编译 / 验证结果

- 23 个 locale 构建及 hash `e907e1e2a988` 检查通过。
- Node 新桥 197 项、旧桥 53 项通过。
- 真实 Chrome 44 项通过。
- 新/旧 Featured Python 43 项通过。
- `{code}`/TT 发布核心 237 项，链接与 app 合同 22 项通过。
- Python/Node 语法和 `git diff --check` 通过。
