# 测试报告

## 测试结论

通过。GitHub-first 生产部署、公网、timer、LKG 故障注入和主服务无重启
验收均完成，无未解决 P0/P1。

## 测试范围

- featured 排行、固定生产源、SQL 参数和元数据预算。
- 元数据筛选、公开字段、32 KiB 上限、原子 LKG。
- Nginx/系统服务契约、既有 resolver 回归。
- 390×844 真实 Chromium 动态卡片与 W2A 点击。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Python featured + resolver | 43 | 43 | 0 | 0 |
| Node 页面逻辑断言 | 53 | 53 | 0 | 0 |
| Playwright 关键流程 | 4 | 4 | 0 | 0 |
| 生产只读 Top20 SQL canary | 2 | 2 | 0 | 0 |
| 需求验收用例 | 29 | 29 | 0 | 0 |

## 缺陷情况

- BUG-001：真实 MySQL 对早期 alias 排序报 1247；发布前发现并修复，
  未进入生产。
- 当前无未解决 P0/P1。

## 验证证据

- Python：43/43，包含 host/port/database fail-close、只读检查、非法 ID
  正则参数、metadata 预算、原子失败保留旧文件及搜索回归。
- Node：53/53，包含固定 W2A 参数、透传、精确 5 条、非法/未来时间和
  72 小时陈旧。
- Playwright：
  `2026-07-26` 动态缓存渲染 5 个链接；点击 `BQ3Y3JcLWA` 前先请求同源
  resolver，再到标题为 `Wrong Sister in His Bed` 的 W2A 页；URL 保留
  `af_adset_id=XXX`，初始 href 为同页安全锚点，本页无 JS/CSP error。
  resolver 404 时留在 `/tt` 并显示 `Story unavailable`；featured API
  503 时回退 5 个不可点击人工卡；首图失败时占位为 `grid` 且链接仍保留。
- 生产只读 SQL：EXPLAIN `key=as/type=ref`；含 LIMIT 前正则的 Top20
  连续两次 1.487s / 1.408s，签名均 `2143235fdcee2b8a`。
- 生产快照：
  `source_date=2026-07-26`、5 条、1,102 bytes、无 `spend`；
  SHA-256 `37e3a126a258e03b89ec743f08300e9d5582dc07f92916349b45c7dec2f5b2df`。
  不存在日期的只读 dry-run 以非零退出，前后 hash 相同；第二次正常刷新
  `changed=false`。
- 公网接口：HTTP 200、ETag、`public,max-age=300`；Windows 侧 5 次
  TTFB 0.929–1.008s，CPU 本机 Nginx 5 次 TTFB 0.134–0.265ms，
  证明用户链路不等待数据库。
- systemd：独立用户与 0600 env，安全评分 3.7/OK；timer enabled/active，
  15:30 首次计划任务自动触发，8 秒成功、`changed=false`、hash 不变，
  下一次 18:00；主 API active、`NRestarts=0`。

## 遗留风险

- insight 在次日中午后仍回填；通过 15:30 主刷与 18:00 对账降低偏差。
- 封面 CDN 单图失败时展示本地占位；不影响卡片点击。
- FakeCursor 不验证真实 MySQL 语法，故生产只读 SQL canary 是长期发布闸门。
- 本期不新增外部通知凭据；刷新失败会进入 systemd failed/journal，前端在
  72 小时后自动回退。若需主动 Feishu 告警，应另行确认通知群和凭据边界。

## 发布建议

已发布，可进入运营测试。保持 15:30 主刷和 18:00 对账；若需主动 Feishu
失败告警，另行确认通知群与最小凭据。
