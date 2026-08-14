# 测试报告

## 测试结论

本地回归全部通过，未发现阻断缺陷。允许进入带新pre-guide备份和原子回滚的生产发布；生产HTTP头、真实Resolver/Featured和双入口将在部署后补充复验。

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
| 合计 | 344 | 344 | 0 | 0 |

## 缺陷情况

无确认缺陷，`bugs/` 为空。SA评审发现的两个P1在QA前已关闭。

## 验证证据

- 静态构建：23 locale，hashed JS `tt-drama-code-search.45ea9a9af6ac.js`。
- WebP：720×960、46,114 bytes、SHA-256 `0b42fbc64ab49e1c58a6f478a8c8f64c90427ce5ef78d06f8b0b145433b2c0`。
- 23种locale均在320px静态首屏无横向溢出，引导summary可见。
- 英文320/390展开、中文、阿拉伯语RTL、破图后Search/Featured均通过。
- 4位code结果继续包含`af_channel=TT`；Featured继续包含`af_channel=Featured`。

## 遗留风险

- 23语尚未逐语种由母语人员人工审校；自动化已保证键完整和320px不溢出。
- 生产WebP响应头、Nginx reload和真实外网路径需在部署后确认。
- 示例图本身为英文；周边23语说明可降低理解成本。

## 发布建议

有条件通过：commit/push精确hash后，按 hashed WebP/JS → Nginx `nginx -t`+reload → 23 locale HTML → current 指针顺序发布；任何P0验证失败立即从本次pre-guide备份回滚。
