# SA 需求与设计评审

## 结论

通过，需求与设计已落实到预发布实现。方案采用固定 URL 的原始 HTML GET、精确 ID 校验和数据盘 SQLite；不依赖浏览器渲染，且不扩大 W2A 归因脚本的执行范围。生产部署与真实 systemd/浏览器验收另行执行。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 处理决策 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SR-001 | P0 | ID 校验 | W2A 对错误 ID 也可能返回 200 | 必须从源码深链解析实际 ID 并区分大小写精确比对 | 已纳入 |
| SR-002 | P0 | 负缓存 | 上游超时或结构变化若写成不存在，会误封真实剧 | 只有明确 404 或已解析的不一致 ID 可写负缓存 | 已纳入 |
| SR-003 | P1 | 源请求 | 接收任意 URL 会形成 SSRF | 固定 HTTPS host/path，仅拼接校验后的 `af_dp` | 已纳入 |
| SR-004 | P1 | 缓存 | API 与 timer 并发写 SQLite 可能重复抓取或锁冲突 | WAL、busy timeout、短事务和跨进程租约 | 已纳入 |
| SR-005 | P1 | 数据盘 | 根盘空间不足、启动时挂载失效或长驻 API 运行中数据盘掉载时可能误写根盘 | 首次检查挂载点、UUID、设备、空间和软链接并记录父目录 `st_dev`；每次 connect 前后复查软链接、父目录和设备号，变化即 fail closed | 已纳入 |
| SR-006 | P1 | 接口兼容 | W2A 不提供语言和集数 | 保留旧字段并返回安全默认值，不改变状态码和包装 | 已纳入 |
| SR-007 | P1 | Featured | 新资源源故障不能清空线上精选剧 | 继续使用完整 5 条的 last-known-good 原子快照 | 已纳入 |
| SR-008 | P2 | 隐私 | 原始 HTML 包含归因、Pixel 和深链数据 | 只保存白名单字段，不保存原始 HTML 或完整深链 | 已纳入 |
| SR-009 | P2 | 预热规模 | 候选异常膨胀可能冲击 W2A | 5001 探针、超过 5000 整轮失败、单轮最多 500 | 已纳入 |
| SR-010 | P1 | 多落地页隔离 | 仅以 Content ID 为主键会污染不同 landing | 缓存和租约统一使用 `(landing_id, content_id)` 复合主键 | 已实现 |
| SR-011 | P2 | 预热公平性 | 每轮固定头部 500 部会让尾部候选长期不被处理 | 使用持久化 cursor 轮转，且新鲜命中不访问源站 | 已实现 |
| SR-012 | P1 | 接口兼容 | 将内部缓存状态直接暴露会扩大旧 API 枚举 | `ORIGIN_FILL/NEGATIVE_FILL -> MISS`，`DISK_HIT -> HIT` | 已实现 |
| SR-013 | P1 | 共享文件权限 | API 与离线用户创建的 SQLite/WAL/SHM 可能互相不可写；给整个单体 API 设置 UMask 又会改变无关模块文件权限 | 主 API drop-in 不设置全局 `UMask`；state 以 `tt-drama-featured:tt-drama-featured`、`2770` 固定创建，缓存模块只将 DB、`-wal`、`-shm` 显式规范为 `0660` | 已实现 |
| SR-014 | P1 | 候选 SQL | 可配置任意 insight 表/索引会扩大只读范围或触发错误执行计划 | 固定 `kunlunads_dev.ads_custom_source_insight` 和索引 `as`，其他值 fail closed | 已实现 |
| SR-015 | P2 | 排名与失败公平性 | 按 ID 重排会破坏花费优先级；单纯轮转会延后瞬时失败项；无限重试又会挤占正常候选 | cursor v2 保持花费排名，以 `next_content_id` 接续、目标消失时用 `next_index` 兜底，并使用有界 retry backlog；每轮为正常轮转保留位置 | 已实现 |
| SR-016 | P0 | 掉载竞态 | 仅在 connect 前复查，数据盘可能在检查通过后、SQLite 打开前掉载 | `sqlite3.connect` 返回后、执行 PRAGMA/SQL 前再次核对路径和已记录 `st_dev`；失败时关闭连接，禁止写入 | 已实现 |

## 决策记录

- `view-source:` 不进入服务端请求；程序直接 GET 正常 HTTPS URL，得到同一份原始 HTML。
- 封面只缓存 CDN URL，不下载或代理图片。
- 最近投放定义为上海时区最近 3 个自然日（含今天）累计花费大于 0。
- 预热和公开 resolver 必须调用同一套 client/parser/cache/service，禁止复制两套解析逻辑。
- 解析失败与内容不存在必须使用不同错误分类。
- 源 URL 只在请求时临时构造；SQLite 不保存 `source_url`、原始 HTML 或完整深链。
- 描述元素必须存在，但元素文本允许为空。
- Featured 的 MySQL 查询只保留昨日花费排名，不再查询剧库元数据。
- 普通预热硬上限为 500；只有显式 bootstrap 可到 3000。
- 完整 mount/UUID 校验只在首次初始化执行；后续每次 connect 前后使用已记录父目录 `st_dev` 做轻量但强制的存储身份复查。

## PM 修订确认

以上问题均已写入 `requirements.md`、`dev-plan.md` 和 `test-cases.md` 并在本地实现；最终本地审计无剩余 P0/P1/P2。生产 Linux 与发布证据仍待补充。
