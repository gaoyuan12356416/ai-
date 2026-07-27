# 016.tt-featured-top-spend 需求与技术设计

## 背景

`/tt` 页面底部的 `Recently featured` 当前由前端写死 5 张装饰卡片，
没有真实 `content_id`，也不能点击继续观看。用户希望这里每天展示
Dramawave W2A 昨日花费最高的几部剧，并且点击卡片可进入对应 W2A
落地页。

## 目标

- 每天离线读取前一上海自然日的 Dramawave W2A 花费，按剧聚合并稳定排序。
- 将前 5 个可投放且有有效封面的剧写入 CPU 服务器本地最后成功快照。
- `/tt` 访客只读取同源本地 JSON，不在页面请求链路查询远端数据库。
- 卡片点击复用现有 `createTarget()`，保留 `af_adset_id` 等合法透传参数。
- 远端查询失败、数据不足或写文件中断时继续服务上一份完整快照。

## 范围

### 包含

- 只读查询 `kunlunads_dev.ads_custom_source_insight`：
  `product='Dramawave'`、`app_id='[w2a]drama-double'`、
  `data_source=6`、`dt=上海昨日`，默认汇总该范围内全部 platform。
- 按 `data_source_id/content_id` 汇总 `SUM(spend)`，以
  `spend DESC, content_id ASC` 稳定排序，先取 20 个候选。
- 使用 `ads_drama_resource` 的 `content_id` 索引校验固定
  `app_id=1479`、有效剧集、剧名和白名单 HTTPS 封面，最终展示 5 个；
  少于 5 个则本轮失败且不覆盖旧快照。
- 数据盘原子 JSON 快照、systemd oneshot/timer、Nginx 精确同源 JSON 路由。
- `/tt` 动态卡片、加载/失败状态、可访问性和真实浏览器点击验证。

### 不包含

- 修改广告、预算、投放状态或任何远端数据库记录。
- 页面打开时实时查询 `ads_custom_source_insight` 或 `ads_drama_resource`。
- 在公开 JSON、DOM 或 URL 中暴露具体花费金额。
- 修改搜索 resolver 的错误 ID 校验、W2A 三个固定核心参数或透传规则。
- 按国家、语言、优化师或单一平台做个性化排行。

## 用户故事 / 业务规则

1. 业务日期按固定 `Asia/Shanghai` 计算，不依赖服务器或访客浏览器时区。
2. 排名范围是 Dramawave 的 `[w2a]drama-double` 昨日总花费；当前线上该范围
   包含 platform `0` 和 `3`，本期不额外限定单一 platform。
3. 聚合键必须是非空、格式合法的 `data_source_id/content_id`，不能按素材 ID
   排名，也不能把大小写不同的 ID 静默改写；合法格式过滤必须在 SQL
   `LIMIT 20` 之前完成。
4. 只有固定 app `1479` 下存在有效剧集、非空剧名和白名单 HTTPS 封面的候选
   才能进入快照。
5. 快照只包含 `content_id/title/cover_url/language/episode_count` 等公开字段；
   `spend` 仅参与服务端排序，不写入公开文件；卡片顺序按同日确定性种子打散，
   不直接公开相对花费名次。
6. 生成器在全部查询、校验和 JSON 序列化成功后，才在同目录 `fsync` 并
   `os.replace`；任何失败都保留 last-known-good。
7. 缓存、运行目录和部署备份放在已验证挂载的
   `/mnt/data-disk/tt-drama-featured`；挂载、UUID、可写性或
   空间检查失败时 fail closed，禁止回落到当前已使用 92% 的根盘。
   oneshot 使用独立 `tt-drama-featured` 系统用户和仅含只读 DB 配置的
   `/etc/tt-drama-featured.env`，不继承主服务的整份敏感环境。
8. timer 每天 `15:30` 做主刷新，`18:00` 用同一昨日日期再对账一次；
   部署时先手工生成并验证快照，再启用 `Persistent=true` timer。
9. 当前快照 `source_date` 等于上海昨日时标题显示
   `Yesterday's top stories`；旧快照仍可展示，但标题降级为
   `Featured stories`，不得误称昨日。
10. 卡片使用真实 `<a>`，目标由现有 `createTarget(content_id, location.search)`
    生成并暂存在 `data-target-url`；初始 `href` 只指向同页安全锚点，普通点击
    先通过现有 resolver 再自动导航，且中键/长按不会绕过校验直达 W2A；
    核心键不可覆盖，合法重复追踪参数保持原顺序。
11. JSON 不可用、超时或有效卡片为 0 时，前端回退到当前人工审核静态卡片，
    但静态卡片不伪造跳转；搜索主流程不受影响。
12. 动态快照的 `generated_at` 超过 72 小时、未来超过 24 小时，或
    `source_date` 晚于上海昨日/落后超过 72 小时时不再展示，回退人工静态
    卡片；这样长期刷新失败或异常时间不会无限使用已下架剧。

## 交互与流程

```text
tt-drama-featured.timer
  -> refresh_tt_drama_featured.py
  -> 63350 只读副本：昨日 W2A 按 content_id 聚合 Top 20
  -> ads_drama_resource 批量校验 app 1479 / 剧集 / 剧名 / 封面
  -> 5 个有效候选
  -> 数据盘同目录临时文件 + fsync + 原子替换 current.json
  -> Nginx GET /api/public/tt-drama/featured 直接返回本地 JSON
  -> /tt fetch 同源 JSON
  -> 生成可横滑、可点击的 5 张卡片
  -> createTarget() 拼固定 W2A 参数及合法透传参数
```

## 技术设计

### 影响模块

- `features/tt_drama_featured/service.py`：时区、只读查询、候选校验和原子快照。
- `scripts/refresh_tt_drama_featured.py`：环境配置、数据盘门禁与单次刷新入口。
- `static/tt-drama-search.js`、`static/tt-drama-search.html`：动态卡片和交互。
- `deploy/nginx/tt-drama-search.conf`：精确本地 JSON 路由。
- `deploy/tt-drama-featured.service`、`.timer`：离线刷新。
- `.env.example`：非敏感配置说明。

### 数据结构

公开快照：

```json
{
  "schema_version": 1,
  "source_date": "2026-07-26",
  "generated_at": "2026-07-27T18:00:03+08:00",
  "items": [
    {
      "content_id": "l9rP6ey2CB",
      "title": "Example Drama",
      "cover_url": "https://static-v1.mydramawave.com/example.jpg",
      "language": "en",
      "episode_count": 80
    }
  ]
}
```

文件不包含花费、数据库身份、SQL、内部路径或错误堆栈，大小硬限制 32 KiB。

### API / 接口

`GET /api/public/tt-drama/featured`

- 无请求参数，无鉴权。
- Nginx 从数据盘精确 alias 本地 `current.json`。
- `Content-Type: application/json`。
- `Cache-Control: public, max-age=300, stale-while-revalidate=3600`。
- 支持浏览器的 ETag/Last-Modified；不代理到 Python，不访问远端数据库。

### 异常与边界

- 只读端点不是 `@@read_only=1`、连接/查询超时：本轮失败，不改快照。
- host、port 或 database 不是固定的
  `101.32.56.53:63350/kunlunads_dev`：任务拒绝运行。
- 昨日无数据、候选少于 5、元数据歧义或封面不安全：不改快照。
- 候选固定最多 20；元数据返回超过每个候选 500 行的总预算：本轮失败。
- 临时文件写入/fsync 或 `os.replace` 失败：旧文件保持可读；替换后的目录
  fsync 若不可用则记录 durability warning，但新文件仍是完整原子 JSON。
- 快照 JSON 缺字段、版本不支持、ID/封面非法：前端逐项拒绝并回退静态卡。
- 快照 `generated_at` 无效或陈旧超过 72 小时：前端回退静态卡。
- 请求超过前端 2 秒：停止等待并回退，不阻塞搜索。
- 访客传入保留参数、超长参数或非法参数：继续由现有透传过滤器丢弃。

## 验收标准

- 昨日排行查询仅走
  `101.32.56.53:63350/kunlunads_dev`，且会话与实例均只读。
- 生产快照恰有 5 个有效剧，`source_date` 为目标昨日，公开响应不含 `spend`。
- 连续读取本地 API 无需数据库，响应稳定且页面不阻塞封面加载。
- 手工制造刷新失败后，生产快照 SHA-256 不变。
- `/tt?af_adset_id=XXX` 点击任一卡片，最终 W2A URL 包含正确
  `af_dp`、固定 `c=TTpost`、固定 `af_c_id=0001` 和透传
  `af_adset_id=XXX`。
- 错误/保留追踪参数不能覆盖三个核心键。
- 390×844 真实浏览器中横滑、键盘焦点、封面回退和点击均通过；
  控制台无 CSP/JS 错误。
- timer 激活且下一次运行时间正确，主 API 无需因日更任务重启。
- 部署记录包含 GitHub commit、数据盘 release/backup、快照 hash、
  Nginx 校验、timer 状态和精确回滚步骤。

## 风险与待确认

- 线上证据显示昨日 insight 在当天 12:00 后仍大量回填，因此不沿用 10:00
  的最终口径；采用 15:30 主刷新和 18:00 对账，早间继续展示上一成功快照。
- 本期“昨日最大”是 W2A 全 platform 总花费，不等同于 Facebook 单平台排行；
  若后续要分渠道，需要新增显式配置和页面文案。
- 公开文件由 Nginx 用户读取，目录只能开放遍历与该 JSON 的只读权限，不能把
  `.env`、日志或包含花费的审计文件放进同一 public 目录。

## 变更记录

- 2026-07-27：新建需求；根据生产只读核验确定 W2A 全 platform 聚合、数据盘
  LKG、15:30/18:00 刷新、确定性乱序和公开花费隔离。
