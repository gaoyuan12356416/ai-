# 011.ad-control-v3-copy-name-utc8 需求与技术设计

## 背景

AI 自动调控 V3 已支持 Facebook Campaign、Ad Set、Ad 的真实复制和执行日志。当前复制对象沿用 Meta 自动生成的名称，无法直接识别 AI 复制批次；V3 审计时间以 UTC 无时区字符串存储，浏览器会按客户端本地时区解释，日期筛选也按 UTC 自然日，导致 UTC+8 用户看到的时间和日统计可能偏移。

## 目标

1. 所有由 V3 复制创建的 Campaign、Ad Set、Ad 名称追加 `[*copybyAI*MMDDHHmm]`。
2. 后缀时间固定按 UTC+8，例如 `07171455` 表示 7 月 17 日 14:55。
3. V3 规则列表、执行日志、详情时间线、接口时间字段和日期筛选统一按 UTC+8 统计展示。
4. 保持现有 UTC 审计存储、Meta 幂等、PAUSED 隔离、created_data/lineage 关联和账号本地调度语义不回退。

## 范围

### 包含

- Facebook V3 Campaign / Ad Set / Ad 三种复制粒度产生的全部新对象。
- 同一复制树使用同一个 UTC+8 分钟标记。
- 复制后 PAUSED 状态下重命名、回读名称/状态并写入 created_data。
- V3 动态页和 `/api/ad-control/v3/*` JSON 响应中的审计时间。
- V3 执行日志 `date_from/date_to` 按 UTC+8 自然日换算为 UTC 存储边界。
- V3 runner 标准输出增加 UTC+8 执行时间和时区标识。

### 不包含

- 不修改 V1/V2 页面、日志或调度时间语义。
- 不修改广告账号本地时区的投放计划判断。
- 不改历史数据库值、不做 DDL/数据回填。
- 不修改 Meta 素材、Creative 名称或来源对象名称。

## 用户故事 / 业务规则

- 复制 Campaign 时，新 Campaign、其全部新 Ad Set、全部新 Ad 均追加同一后缀。
- 复制 Ad Set 时，仅新建的承载对象追加后缀；复用的来源 Campaign 不改名。
- 复制 Ad 时，仅新建的承载 Campaign/Ad Set（如有）及新 Ad 追加后缀；复用父对象不改名。
- 再次复制已带历史后缀的对象时，在当前来源名称末尾继续追加新的后缀。
- 名称过长时保留后缀，安全截断来源名称部分。
- 任一级重命名或回读不一致，整个 intent 保持 PAUSED 并进入隔离，禁止激活。
- UTC+8 展示固定使用 `Asia/Shanghai`，不依赖浏览器、服务器或广告账户时区。

## 交互与流程

1. intent 预占时生成一次复制时刻。
2. 以该时刻生成 `[*copybyAI*MMDDHHmm]`。
3. Meta 复制新对象为 PAUSED。
4. 逐层更新名称并回读校验名称、来源关系和 PAUSED 状态。
5. 按已校验名称写入 created_data/lineage，完成校验后才允许激活。
6. API 将 UTC 存储时间序列化为带 `+08:00` 偏移的 ISO 时间；前端固定按 `Asia/Shanghai` 格式化。

## 技术设计

### 影响模块

- `features/ad_control_v3/live_execution.py`：统一后缀、三层重命名、回读与隔离。
- `features/ad_control_v3/time_utils.py`：UTC 存储、UTC+8 显示、日期边界和名称后缀的唯一实现。
- `features/ad_control_v3/routes.py`：API 时间序列化与时区响应头。
- `features/ad_control_v3/repository.py`：UTC+8 自然日筛选边界。
- `features/ad_control_v3/service.py`：元数据声明时间标准。
- `features/ad_control_v3/assets/app.js`：明确解析 UTC 并固定 UTC+8 展示。
- `scripts/ad_control_v3_runner.py`：UTC+8 runner 日志时间。

### 数据结构

无 DDL。数据库继续保存 UTC 无时区审计值，避免破坏过期判断、幂等和历史数据；UTC+8 只用于命名、API 表达、页面展示和自然日统计边界。

### API / 接口

- V3 JSON 响应头增加 `X-Ad-Control-Timezone: UTC+8`。
- 已知审计字段输出为 `YYYY-MM-DDTHH:mm:ss.ffffff+08:00`。
- `/meta` 增加 `time_standard`，声明 storage=`UTC`、display=`UTC+8`、IANA=`Asia/Shanghai`。
- 接口路径、请求字段和数据库结构不变。

### 异常与边界

- 空来源名称使用安全占位名称后再追加后缀。
- 名称后缀始终完整保留，来源部分按固定最大长度截断。
- 时区字符串无法解析时不伪造时间；API 保留原值，复制命名只接受受控 `datetime` 时钟。
- 日期筛选结束边界使用次日 UTC+8 00:00 的排他 UTC 值，避免 23:59:59.999999 漏数。
- 纯日期 `YYYY-MM-DD` 在前端按业务日期原样显示，不做跨日转换。

## 验收标准

- 固定时钟 `2026-07-17 06:55 UTC` 生成 `[*copybyAI*07171455]`。
- Campaign、Ad Set、Ad 各粒度的所有新对象都带同批后缀，复用父对象不改名。
- created_data 的三级名称与 Meta 回读名称一致。
- 重命名失败时不写 ACTIVE，不重新发起复制，不丢失 intent/新对象 ID。
- API 时间带 `+08:00`，页面在任意浏览器时区均展示相同 UTC+8 时间。
- 选择 7 月 17 日查询时，后端 UTC 边界为 7 月 16 日 16:00（含）至 7 月 17 日 16:00（不含）。
- V3 全量回归、共享功能回归、服务器精确提交测试和生产健康检查通过。

## 风险与待确认

- Meta 每个新对象增加一次名称更新写请求；计入实际 `meta_write_count`，现有限额不绕过。
- 本期不批量改历史记录；历史 UTC 值通过同一输出转换立即按 UTC+8 展示。
- 生产不主动制造额外 ACTIVE 复制；使用 Stub/PAUSED Canary 和既有隔离链路验证。

## 变更记录

- 2026-07-17：建立需求，确认 UTC 存储不变、UTC+8 统计展示、三层复制后强制重命名。
