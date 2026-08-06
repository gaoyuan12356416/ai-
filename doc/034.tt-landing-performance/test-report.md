# 测试报告

## 测试结论

本地功能、兼容、安全、真实浏览器和生产验收全部通过。中文首屏由服务器
直接返回，已消除“先显示英文、约 11 秒后再切中文”的可见延迟；搜索、
Featured 和四字符 code 路由合同未发生回归。

## 测试范围

- 23 语言静态首屏、请求头白名单与无 JS 首屏。
- 单语言 schema v3、WebP 生成/回退、LKG 与全量基础设施失败。
- 横向滑动、拖动抑制、Search/Featured 完整 W2A 跳转。
- 四字符 code 唯一性、`{code}` 冻结/重试和 Redis/SQLite resolver 回归。
- 生产 Nginx、HTTP 响应头、真实数据、独立 systemd 监听与回滚快照。

## 本地执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Featured Python 新/旧 | 43 | 43 | 0 | 0 |
| TT 发布/code 宏核心 | 237 | 237 | 0 | 0 |
| TT 链接/app 合同 | 22 | 22 | 0 | 0 |
| 新/旧前端合同 | 250 | 250 | 0 | 0 |
| 真实 Chrome | 44 | 44 | 0 | 0 |
| 合计 | 596 | 596 | 0 | 0 |

## 生产实机结果

使用真实 Chrome、`zh-CN`、390x844 视口、禁用缓存执行 10 次独立冷启动
采样。中位数如下：

| 指标 | 中位数 | 说明 |
| --- | ---: | --- |
| HTML response end | 1.024 秒 | 首屏 HTML 已是中文 |
| FCP | 1.284 秒 | 独立复核样本；首次可见即中文 |
| DOMContentLoaded | 2.046 秒 | 没有标题二次切换 |
| 5 条动态榜单完成 | 3.072 秒 | 只取 `zh-tw.json` |
| 5 张封面完成 | 5.309 秒 | 全部为同源 WebP |

公网存在一次明显网络离群样本：HTML response end 3.774 秒、DCL 6.280 秒、
图片完成 10.782 秒。即使该样本中，服务器返回的首屏仍直接为中文，不再
等待 JS 翻译。性能数字用于对比，不作为固定 SLA。

## 网络与资产证据

- 构建器输出 23 个语言 HTML、唯一 hash JS
  `tt-drama-code-search.e907e1e2a988.js`。
- hash JS gzip 后正文 27,793 字节，缓存一年并带 `immutable`。
- 页面只请求一个 `zh-tw.json`，未请求旧的全语言 JSON。
- 5 张同源 WebP 全部 200、尺寸 236x338，独立复核总传输约 105 KiB。
- 原全语言 v2 endpoint 继续 200/gzip；`/tt-code` 与 `/tt` 行为一致。
- 首次资产任务：22 个语言文件、109 次封面转换成功、104 个去重 WebP；
  第二次运行无文件变化，证明幂等。

## 跳转与 `{code}` 证据

- Search 与 Featured 均在 Chrome 内完成 resolver 请求并进入预期 W2A URL；
  最终域名请求被拦截，没有真实外跳。
- Featured 首条剧没有发布历史，按已确认规则使用通用参数
  `c=TTpost&af_c_id=0001`，并分别写入 `af_channel=Search/Featured`。
- 现网已有四字符 code 的只读解析结果为 `query_type=code`、
  `route_mode=code_exact`、八个参数完整、`af_channel=TT`。
- 当前活动 TT Post release 直接运行 17 项 code route/macro 测试全部通过；
  其中包含 `{code}` 精确替换、延迟冻结、大小写校验、唯一分配、容量回收、
  Redis 回退及发布状态语义。
- 没有为测试触发真实 TikTok 发布；宏结论来自活动生产代码测试和现有发布
  记录的只读解析，而不是新建 canary。

## 缺陷情况

代码评审提出 5 项问题，均在部署前修复并通过独立复核；无开放缺陷。

## 服务健康

- `nginx -t` 通过。
- `nginx`、`drama-material-api`、`tt-code-redis`、`tt-post-service`、原
  Featured timer 和新资产 `.path` 均为 active。
- Nginx、主 API 和 Redis 的 `NRestarts=0`；本次仅 reload Nginx。
- 部署前备份 SHA 清单在部署后再次全部校验通过。

## 遗留风险

- 公网耗时仍受用户网络 RTT 影响，本次修复不能消除跨境网络波动。
- 浏览器首屏已不依赖语言识别 JS，但动态榜单仍需要一次约 1 秒级公网请求；
  若还需进一步降低，可在独立需求中增加离用户更近的 CDN/边缘缓存。

## 发布结论

生产验收通过，可以继续使用原地址 `https://ai.yingliangads.com/tt`。
