# 029 TT 自动发布关闭品牌披露与短链缩短

## 背景

自动发布任务会冻结账号设置中的 `brand_content_toggle`、`brand_organic_toggle`，因此账号设置曾开启“自有品牌”时，自动发布也会携带品牌披露。TT 短链当前用 19 位保留命名空间，例如 `/s2l/8000000000000000006.html`，可读性差。

生产核对同时确认：`/s2l/1.html` 等纯数字路径已由 X 发布系统占用，`/s2l/6.html` 已存在，不能直接复用，否则会破坏历史 X 链接。

## 目标

- 所有 TT 自动队列发布强制发送 `brand_content_toggle=false`、`brand_organic_toggle=false`。
- 新 TT 自动队列短链使用任务表自增 ID：`https://gy.g2flow.com/s2l/tt/{queue_id}.html`。
- 保留历史 19 位 TT 链接和现有 X 纯数字链接，不迁移、不覆盖。
- 不改变立即发布测试（direct-test）的独立设置和历史短链。

## 范围

### 包含

- 自动队列冻结时双披露开关固定为 false。
- 最终正式发布边界再次把队列双披露字段归零，覆盖部署前尚未初始化的旧自动队列。
- 新自动短链以 `tt_post_queue.id` 生成 `/s2l/tt/{id}.html`。
- 链接校验、W2A 参数、不可变 HTML 写入和 Nginx 路由兼容新旧格式。
- 页面短链预览和相关合同测试更新。

### 不包含

- 不改历史已发布任务、TikTok 既有 Post 或历史短链文件。
- 不覆盖 X 的 `/s2l/{id}.html`。
- 不改变 direct-test 的披露设置或 19 位历史命名空间。
- 不触发真实 TikTok 发布验证。

## 业务规则

1. 所有写入 `tt_post_queue` 的新任务，披露字段必须为 0；账号设置中即使为 true 也不得带入自动队列。
2. 正式 `begin_publish` 必须在 TikTok init 之前原子归零双披露字段，并把归零后的快照传给 GPU。
3. 新自动短链 ID 必须严格等于同一条 `tt_post_queue.id`。
4. SQLite `BEGIN IMMEDIATE` 下读取 `sqlite_sequence` 预测下一自增 ID；插入后必须断言 `lastrowid` 一致，否则整事务回滚。
5. 历史 19 位 TT URL 继续通过原目录与原 Nginx 规则访问；新 URL 写入 `s2l/tt/{id}.html`。
6. exact idempotency replay 必须接受历史长链接快照，也必须稳定返回新短链快照。
7. 新路径只允许正整数 ID、GET；非数字、根目录和写方法拒绝。

## 技术设计

### 影响模块

- `features/tt_posts/links.py`
- `features/tt_posts/core.py`
- `features/tt_posts/service.py`
- `deploy/nginx-tt-short-domain-location.conf`
- `static/tt-post-pool.html`
- TT Post 测试脚本

### 数据结构

无 schema 变更。复用 `tt_post_queue.id`、`short_link_id`、`short_url` 字段。

### 接口

API 结构不变。新自动任务响应中的 `short_link_id` 为 queue ID，`short_url` 为 `/s2l/tt/{queue_id}.html`。

## 验收标准

- 账号设置为自有品牌或第三方品牌时，新自动 queue 两字段仍为 false。
- 最终 GPU publish payload 两字段均为 false。
- queue ID 6 对应 `/s2l/tt/6.html`，文件路径为 `s2l/tt/6.html`。
- 历史 `/s2l/8000000000000000006.html` 仍可校验、重放和访问。
- X `/s2l/6.html` 内容/hash 不变。
- 全量 TT 测试通过，生产验收不创建 Post。

## 风险

- 预测 SQLite 自增 ID 必须在同一 `BEGIN IMMEDIATE` 事务内完成并校验，避免并发错配。
- Nginx 新路径必须与旧 TT 19 位规则、X 纯数字规则并存。

## 变更记录

- 2026-08-04：完成现网冲突核对和技术设计。
