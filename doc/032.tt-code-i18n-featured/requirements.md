# 032.tt-code-i18n-featured 需求与技术设计

## 背景

独立页面 `/tt-code` 已支持四字符 code、剧 ID 搜索和 Featured 点击跳转。本次只升级该独立页面：调整首屏标题、按浏览器语言翻译整页，并把 Featured 从全局榜单升级为按剧语言的昨日消耗 Top 榜单。

## 目标

1. 英文主标题精确为 `Enter the code and keep watching`，删除标题下方说明段落。
2. 页面根据 `navigator.languages` / `navigator.language` 自动选择界面语言，无法识别或不支持时使用 `en`。
3. 每日定时从已验证只读数据源计算各剧语言的消耗 Top，榜单只落本地存储盘，不在公网数据中暴露消耗。
4. 用户只看到与浏览器语言匹配的 5 条榜单；该语言无完整榜单时回退英文榜单。

## 范围

### 包含

- `/tt-code` HTML、JavaScript、静态 Nginx 路由。
- Featured 离线刷新服务的分语言查询、校验、原子落盘和 last-known-good 语义。
- 页面静态文案、动态状态、占位文案和 ARIA 文案的本地字典翻译。
- Node、Python、Nginx 合约、浏览器和线上回归测试。

### 不包含

- 不修改旧 `/tt` 页面、旧 `/api/public/tt-drama/featured` 响应和旧 resolver。
- 不修改 code / 剧 ID 搜索与最终 W2A 的 8 个 AF 参数合同。
- 不接入在线机器翻译服务，不翻译剧名和剧情简介。
- 不改变榜单刷新频率；沿用现有上海时区每日定时任务。

## 用户故事 / 业务规则

- 作为英文浏览器用户，我看到英文界面和英文剧消耗 Top 5。
- 作为中文浏览器用户，我看到中文界面和中文剧消耗 Top 5。
- 作为未支持语言用户，我仍看到可用的英文界面和英文榜单。
- 页面语言解析顺序为浏览器语言数组顺序；地区标签先精确匹配，再匹配基础语言，最后回退 `en`。
- 简体中文浏览器标签使用 `zh-hans` UI，繁体中文浏览器标签使用 `zh-hant` UI；生产剧语言目前只有统一的 `zh-tw` 中文桶，因此两类中文 UI 都选择该中文剧榜，不转换剧名繁简体。
- 每个语言桶必须恰好 5 条、剧 ID 唯一、封面域名安全、生成时间有效；不完整桶不发布。
- Featured 点击仍以 `source=Featured` 解析，直接搜索仍以 `source=Search` 解析；只有 `found=true` 且目标 URL 校验通过才跳转。

## 交互与流程

1. 页面初始化时解析浏览器语言并应用本地文案字典，同时设置 `html.lang` 和书写方向。
2. 页面请求一个静态分语言榜单快照，在浏览器内只选择匹配桶；缺桶则选择 `en`。
3. 渲染 5 张卡片，保持左右滑动、箭头、键盘和单击跳转。
4. 定时刷新在单次只读连接内构建全部可发布语言桶；先完整校验，再原子替换分语言快照。
5. 原有全局 Featured 快照仍按原流程生成，保证旧 `/tt` 行为不变。

## 技术设计

### 影响模块

- `static/tt-drama-code-search.html`
- `static/tt-drama-code-search.js`
- `features/tt_drama_featured/service.py`
- `scripts/refresh_tt_drama_featured.py`
- `deploy/nginx/tt-drama-code-search.conf`
- `deploy/tt-drama-featured.service`
- 相关 Node / Python / Nginx 合约测试

### 数据结构

新增本地文件 `/mnt/data-disk/tt-drama-featured/public/current-by-language.json`：

```json
{
  "schema_version": 2,
  "source_date": "2026-08-04",
  "generated_at": "2026-08-05T15:30:00+08:00",
  "default_language": "en",
  "rankings": {
    "en": [{"content_id":"...","title":"...","cover_url":"https://...","language":"en","episode_count":60}],
    "zh-tw": [{"content_id":"...","title":"...","cover_url":"https://...","language":"zh-tw","episode_count":60}]
  }
}
```

每个 `rankings` 值必须恰好 5 条。快照禁止出现 `spend` / `spend_n`。

### API / 接口

- 新增只读静态接口 `GET /api/public/tt-drama/featured-by-language`，映射到上述文件，允许短时公共缓存。
- 保留 `GET /api/public/tt-drama/featured` 原样不动。
- 搜索和 Featured 点击继续使用 `GET /api/public/tt-code/resolve`，不增加语言参数。

### 异常与边界

- 分语言快照请求失败、格式错误或超时：页面使用当前 UI 语言的 5 条不可点击占位卡；未支持 UI 语言时占位文案为英文，不自动跳转。
- 请求语言无完整桶：回退 `en`；`en` 是快照必需桶。
- 某非英文语言不足 5 个有效剧：跳过该桶并保留整个快照其余完整桶；英文不足 5 条则本次快照整体失败并保留上次文件。
- 刷新过程中任一写入失败：不产生半文件；旧快照继续可读。
- 源数据库仅允许已验证只读主机、端口、库、表、索引和固定产品范围。

## 验收标准

- 英文页面标题完整精确，说明段落不存在，移动端布局无粘连。
- 英文和中文浏览器分别显示对应语言 UI 与 5 条对应语言榜单。
- 未支持语言和缺桶均稳定回退英文。
- 公网快照无消耗字段，且每桶 5 条、无重复 ID。
- 搜索、Featured 单击、拖动、箭头和键盘全部回归通过。
- 旧 `/tt` 文件与旧 Featured API 合同零变更，线上仍可用。
- 部署版本来自 GitHub 精确提交，数据盘有部署前备份和可执行回滚命令。

## 风险与待确认

- 支持语言集合以生产数据中的规范化剧语言值为准；浏览器标签别名固定映射到这些值。
- 数据源语言只允许使用 `drama_language` 并在查询层规范化；素材字段 `language` 存在一剧多值和编码差异，明确禁止用于分榜。

## 变更记录

- 2026-08-05：根据用户确认创建 032 需求，边界为新 `/tt-code` 独立升级、旧 `/tt` 不动。
