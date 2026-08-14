# 测试报告

## 测试结论

本地与生产回归全部通过，未发现阻断缺陷。页面已按GitHub-first流程发布，生产HTTP头、真实Resolver/Featured、无效code、双入口和日志均通过。

## 测试范围

- 23语静态首屏和copy完整性。
- 320×568、390×844、桌面、中文及RTL布局。
- details鼠标/键盘展开、搜索成功隐藏、输入重置和图片404隔离。
- WebP内容hash、hashed JS、Nginx配置合同。
- 既有Search、Featured和W2A参数回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Node语法检查 | 2 | 2 | 0 | 0 |
| 静态构建检查 | 1 | 1 | 0 | 0 |
| Bridge断言 | 233 | 233 | 0 | 0 |
| Chrome浏览器检查 | 107 | 107 | 0 | 0 |
| Diff格式检查 | 1 | 1 | 0 | 0 |
| 生产HTTP/API合同套件 | 1 | 1 | 0 | 0 |
| 生产Chrome端到端套件 | 1 | 1 | 0 | 0 |
| 生产日志/进程检查 | 1 | 1 | 0 | 0 |
| 合计 | 347 | 347 | 0 | 0 |

## 缺陷情况

无确认缺陷，`bugs/` 为空。SA评审发现的两个P1在QA前已关闭。

## 验证证据

- 静态构建：23 locale，hashed JS `tt-drama-code-search.45ea9a9af6ac.js`。
- WebP：720×960、46,114 bytes、SHA-256 `0b42fbc64ab49e1c58a6f478a8c8f64c90427ce5ef78d06f8b0b145433b2c0`。
- 23种locale均在320px静态首屏无横向溢出，引导summary可见。
- 英文320/390展开、中文、阿拉伯语RTL、破图后Search/Featured均通过。
- 4位code结果继续包含`af_channel=TT`；Featured继续包含`af_channel=Featured`。
- 活动release：`b0775bc5cbaac53d47529ac366b05ed744fe5731`；回滚包manifest复验通过。
- 公网 `/tt` 与 `/tt-code` 为200/no-store，`/tt/`为404；WebP为200/image/webp/immutable。
- 真实`83WA`保持8个已发布归因参数；Featured和Content ID保持既有4参数generic fallback及正确channel。
- 生产Chrome冷启动3次：response end 902–1046ms、DCL 1824–1993ms、Featured 2758–2927ms、5图4542–4735ms。
- 同一guide URL每页仅1个网络请求；展开自然宽720，`ZZZZ` 404后Guide保留；Search成功后收起隐藏。
- 验收窗口日志：guide 22次200、新JS 16次200、TT链5xx=0、Nginx error匹配=0。

## 遗留风险

- 23语尚未逐语种由母语人员人工审校；自动化已保证键完整和320px不溢出。
- 示例图本身为英文；周边23语说明可降低理解成本。

## 发布建议

通过。保留本次pre-guide回滚包，继续观察guide/JS 404、Resolver错误率和搜索转化；出现P0异常时按deploy.md原子恢复。
