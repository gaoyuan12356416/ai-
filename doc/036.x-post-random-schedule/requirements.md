# 036 X Post 随机发布

## 目标

X 素材池和 X 短剧池的自动发布设置增加“固定时间 / 随机时间”模式。随机模式由管理员设置每日发布次数，系统每天生成并持久化北京时间随机时间点。

## 业务规则

- 两个池分别配置，模式为 `fixed` 或 `random`。
- 随机次数范围为 1–24；一次代表所选每个账号各发 1 条，因此每日总条数为“账号数 × 次数”。
- 随机时间覆盖北京时间 00:00–23:59，不选择整点，相邻时间至少间隔 60 分钟。
- 随机计划按池和日期持久化；重启、重复轮询和页面刷新不重抽。
- 相邻两日不能使用完全相同的时间表。
- 启用随机模式或修改账号、次数、模板后从次日生效，不补跑当日过去时段。
- 两个池共用账号时，同一日期不得出现相同分钟的发布时间。
- 已冻结批次继续使用冻结的账号、模板和配置版本，后续编辑不能篡改该批次。
- 旧配置迁移后仍为固定时间模式，不自动改变线上排期。

## 模板与短链

- 描述模板继续支持 `{{url}}`、`{{drama_name}}`、`{{desc}}`；短剧池另支持 `{{episode_number}}`。
- `{{url}}` 在日志 ID 预留后替换为 `https://gy.g2flow.com/s2l/<log_id>.html`。
- 队列冻结完整模板；配置修改仅影响后续未冻结批次。

## 技术设计

- `x_post_schedule_config` 增加 `schedule_mode`、`random_daily_count`、`random_effective_date`。
- `x_post_schedule_run` 增加冻结字段 `schedule_mode`。
- 新表 `x_post_schedule_random_plan` 以 `(source_type, run_date)` 为主键，保存配置版本、账号、模板及时间点。
- 迁移为 additive/idempotent，不重写队列、日志、账号令牌或历史配置。

## API

- `GET /api/x-posts/schedule-settings/<source_type>` 返回模式、次数、生效日、今日/明日随机计划和每日总条数。
- `PUT /api/x-posts/schedule-settings/<source_type>` 接收 `schedule_mode` 和 `random_daily_count`；随机模式要求 `publish_times=[]`。

## 验收标准

- 素材池、短剧池均可保存和回显随机设置及计划。
- 上限 24 次可稳定生成，满足时间规则并跨池避碰。
- 服务重启后计划不漂移，次日才生效。
- `{{url}}` 冻结后渲染为唯一日志短链且不残留宏。
- 无真实 X 发帖的离线和浏览器回归通过；线上数据库迁移前后业务数据不变。
