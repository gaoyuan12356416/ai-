# 测试用例

| 编号 | 场景 | 预期 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| TC-001 | 打开 `/tt` | 200、无重定向、标题为新页标题、加载新 JS | P0 | 本地通过 |
| TC-002 | 打开 `/tt-code` | 继续 200，与 `/tt` HTML 一致 | P0 | 本地通过 |
| TC-003 | Nginx exact location | 两份配置合计仅一个 `location = /tt` | P0 | 本地通过 |
| TC-004 | `/tt` 安全与缓存头 | no-store、no-cache、DENY、同源 connect、GET-only | P0 | 本地通过 |
| TC-005 | 旧静态回滚资产 | `tt-drama-search.html/js` 无 diff、旧 API/JS 路由仍在 | P0 | 本地通过 |
| TC-006 | `en-US` 主页面 | 英文标题、无副标题、对应榜单 5 条 | P0 | 本地通过 |
| TC-007 | `zh-CN` 主页面 | 简中 UI、中文榜单 5 条 | P0 | 本地通过 |
| TC-008 | `zh-TW` 与 `ar` | 正确语言、RTL 规则正常 | P1 | 本地通过 |
| TC-009 | 未支持语言 | UI 与榜单回退英文 | P0 | 本地通过 |
| TC-010 | Featured 拖动 | 左右可滑动且不误跳转 | P0 | 本地通过 |
| TC-011 | Featured 单击 | 只解析一次，拦截目标中 `af_channel=Featured` | P0 | 本地通过 |
| TC-012 | 搜索四字符 code | 大小写规范化，已发布记录保持 `af_channel=TT` | P0 | 待线上验收 |
| TC-013 | 搜索剧 ID | 最新记录或通用 fallback，`af_channel=Search` | P0 | 待线上验收 |
| TC-014 | 自动排期含 `{code}` | queue 分配 `[A-Z0-9]{4}`，caption 替换且无宏残留 | P0 | 本地通过 |
| TC-015 | 正式发布请求 | Fake GPU queue.caption 等于冻结 caption | P0 | 本地通过 |
| TC-016 | GPUClient 序列化 | HTTP payload.title 原样等于 caption 且无宏残留 | P0 | 本地通过 |
| TC-017 | 立即测试含 `{code}` | 409 `tt_post_code_macro_queue_only`，不创建任务、不调用 publish | P0 | 本地通过 |
| TC-018 | 生产配置语法 | `nginx -t` 成功后才 reload | P0 | 待部署 |
| TC-019 | 生产双入口一致 | `/tt` 与 `/tt-code` HTML 哈希一致、旧 JS 未覆盖 | P0 | 待部署 |
| TC-020 | 生产服务边界 | TT current、resource current、Redis/服务状态未改变 | P0 | 待部署 |

## 真实浏览器约束

- 测试 Featured 和搜索跳转时必须拦截 `dramawavew2a.com`，只检查目标 URL，不产生真实外链访问。
- 只有 resolver `found=true` 且 W2A 目标验证通过才能观察到导航。
- 至少覆盖桌面尺寸和 390×844 移动尺寸；语言覆盖 `en-US`、`zh-CN`、`zh-TW`、`ar`、未知语言。
