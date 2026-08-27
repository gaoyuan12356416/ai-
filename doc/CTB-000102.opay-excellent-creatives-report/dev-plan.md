# 开发计划

## 开发范围

实现独立 OPay 月度优秀素材静态报表，包括关键词导入、只读日缓存、严格映射与选优、媒体缓存、版本发布、前端、定时任务、测试、文档和生产回填。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 需求与 SA 契约 | Codex | `doc/CTB-000102.*` | 已完成 |
| 关键词工作簿解析 | Codex | `import_keywords.mjs`、版本化 JSON | 已完成 |
| 数据缓存与动态配置 | Codex | Python 生成器、SQLite | 已完成 |
| 选优和审计 | Codex | Python 规则模块 | 已完成 |
| 媒体降级与制作者 | Codex | Python 素材模块 | 已完成 |
| 静态前端与 CSV | Codex | `report.html` | 已完成 |
| 部署单元 | Codex | Nginx/systemd/env | 已完成 |
| 单元/契约/回归 | Codex | `test_*.py`、验证脚本 | 已完成，25/25 自动测试及 32/32 验收用例通过 |
| GitHub 与生产发布 | Codex | 精确 commit/release | 已完成，运行版本 `0cba014b56f1c6394a9d0d3be5d735a370f83659` |

## 编译 / 构建命令

```powershell
python -m py_compile ops\opay-excellent-creatives\opay_excellent_creatives.py
python -m unittest discover -s ops\opay-excellent-creatives -p "test_*.py" -v
python ops\opay-excellent-creatives\validate_frontend_contract.py
git diff --check
```

## 风险与依赖

- 生产只读 MySQL 命令由 `/root/codex_test/opera_product_daily_dashboard.py` 提供，真实凭据不进入仓库。
- 视频无封面时优先 FFmpeg；服务器未提供 FFmpeg 包时由现有 OpenCV 运行库的受控子进程回退，失败仅降级单行。
- Google 精确链路不足时按审计 0 行交付，禁止估算。
- 生产发布前验证数据盘挂载、只读端点、GitHub SSH、Nginx 和旧报表回归。

## 完成记录

- 2026-08-26：独立工作树 `codex/opay-excellent-creatives-report-20260826` 从当前 AI Game Performance 报表分支创建，现有工作树未修改。
- 2026-08-26：使用工作簿运行库只读解析 NG/PK，生成 90 条配置；原 Excel 未改动。
- 2026-08-26：完成生成器、静态页面、Nginx/systemd、25 项自动测试与桌面/390px 浏览器验收。
- 2026-08-26：代码评审修复独立锁、显式关闭继承鉴权及月度维度刷新问题；候选版本可进入生产影子验证。
- 2026-08-26：修复只读代理入口端口护栏误判，完成 2026-07 只读影子与冻结快照回归。
- 2026-08-26：按 GitHub 精确提交发布，回填 2026-01 至 2026-07 共 186 行，启用初版/终版 timer，并完成匿名公开、旧系统、媒体、CSV、移动端和可逆 timer 回滚演练。

## V2 增量开发计划（2026-08-27）

以上完成状态属于V1。以下为V2分工及门禁；最初阶段仅补文档，后续获准新增独立验收脚本/测试和执行本地验证，见本节末。生产回填、commit/push及部署不由本执行者承担，不得据V1的25/25、32/32宣布V2通过。

### V2 范围与并行分工

| 阶段 | 交付及验收条件 | 负责人 | 状态 |
| --- | --- | --- | --- |
| V2-D01 契约 | 七份V2文档；全部候选链、历史FX、B-only、null、schema2、六项公式、CLI/回滚边界一致 | 本次文档执行者 | 已增量补齐并对齐稳定接口 |
| V2-D02 只读预检 | 核验 Google type3/type0、video2/image4 枚举、mapping 连接列/唯一键、账户币种及历史 FX 来源；记录证据不含凭据 | 后端 | 待实施/核验 |
| V2-D03 缓存兼容 | V1表/列不变；GG独立表；一致性clone、冻结preserve、V2新默认缓存名，数据根/旧快照媒体原地不变 | 后端 | 实现方反馈稳定，待独立核验 |
| V2-D04 GG 刷新 | type3映射、type0基准、micros/历史FX、缺FX fail-closed；缺Campaign account-day暂停B；google-only必须refresh | 后端 | 实现方反馈稳定，待独立核验 |
| V2-D05 输出与选优 | GG仅B；素材AF/安装null；conversions仅详情；schema2六项row.metrics，Meta/TT保持冻结结果 | 后端 | 实现方反馈稳定，待独立核验 |
| V2-D06 页面 | 主表/详情/CSV同一metrics；仅金额缺失不影响完整CTR；零/缺失不同，移动端可用 | 前端 | 实现方反馈稳定，待独立核验 |
| V2-D07 开发自测 | 参数/映射/FX/阈值/null/schema/CSV/失败保留清单 | 实现负责人 | 反馈51后端测试、34前端行为契约通过；本节未独立执行 |
| V2-D08 独立验证 | 代码评审、完整用例、1—7 月 Meta/TT 守恒、GG 抽样、UI/CSV/回滚证据；不重写 V1 通过记录 | 独立 QA | 待代码完成后补 sa-code-review/test-report |
| V2-D09 发布 | GitHub 精确 SHA、V1 备份、隔离回填、原子提交、观察及必要回滚 | 发布负责人 | 待独立 QA 与发布授权，不由本次执行 |

允许后端、前端和文档并行；独立 QA 的执行发生在相关代码完整后，发现缺陷回交实现负责人修复再回归。协作者各自保留他人工作，不 reset/checkout 回退、不开新 commit 混入别人的修改。

### V2 数据实施顺序

1. 固定V1源缓存、关键词版本、1—7月快照及Meta/TT回归签名；记录SHA-256/SQLite一致性。data_root保持`/mnt/data-disk/opay-excellent-creatives`，V1 cache旧名不变；V2副本为`cache/opay-excellent-creatives-v2.sqlite3`，shadow为`staging-public-v2`。旧快照/散列媒体原地保留，不新建独立数据根。
2. 用 SQLite 一致性备份克隆到独立 V2 缓存，原库只读。保留 V1 `platform_daily`、`material_daily`、`af_daily`、`daily_audit` 的列结构和 Meta/TT 事实；当前实现新增 `google_insight`、`google_asset_mapping`、`google_month_refresh`、`google_asset_launch`，结构/索引仍待代码评审核验，不依靠原表改 nullable 列。
3. 新 GG 表缓存 type3 资产事实、type0 平台事实、唯一映射与 FX 证据。按日读取并事务化更新 GG 范围，失败不能留下可发布的半日数据；查询只走 63350 且独立检查 `@@read_only=1`。
4. `--google-only --refresh`不刷新Meta/TT/AF原事实；校验冻结快照哈希及旧事实/选优签名后preserve旧行/基准/审计，仅补metrics；缺基线/签名改变即失败。GG维度补充不得改变旧渠道业务字段。`current`切换决定新旧默认缓存，不改env/Nginx/timer。
5. 全部source候选链须合法一致才计exact；空FX候选跳过，正消耗历史USD缺失仍按缺口。月聚合确认asset-day均有同App/账户/日Campaign type0，缺失则该GG月/App的B暂停/CTR为null；仅FX缺失时金额可null、CTR仍可用。素材/Campaign原币缺口分开审计；conversions允许有限非负小数且仅详情，APM页面固定4位、CSV原精度。
6. 显式 `--rebuild` 重算克隆中冻结的 `2026-01`—`2026-07`；无 `--publish` 先生成全部影子快照，再核验完备性/哈希/旧渠道差异。重跑基于 V2 checkpoint，不能再次克隆覆盖已生成缓存。
7. 在隔离输出预演发布失败、schema 1/2 读取与回滚顺序；由独立 QA 使用实际生成 JSON 对账，不能把 fixture 当 payload 自证通过。

### V2 验证命令（计划，未执行）

以下已有入口须在代码完成后由实现/QA 运行；新增 CLI 参数先核对 `--help`。这里只记录命令，不表示它们已覆盖或通过 V2。

```powershell
python -m py_compile ops\opay-excellent-creatives\opay_excellent_creatives.py
python -m unittest discover -s ops\opay-excellent-creatives -p "test_*.py" -v
python ops\opay-excellent-creatives\validate_frontend_contract.py
python ops\opay-excellent-creatives\opay_excellent_creatives.py --help
git diff --check
```

- 需补充测试：clone 一致性/不可覆盖、`--google-only` 缺少 `--refresh`、冻结缺少 `--rebuild`、GG 表隔离、历史 FX 切换/缺失、严格阈值及六项 null/zero；具体测试函数名由实现和独立 QA 记录。
- `validate_regression_snapshot.py`现已支持`--non-google-only`，以实际生成schema2月JSON与冻结V1签名比较Meta/TT，另验证GG/metrics；默认fixture仅2026-07，其余月份逐月对账。不得删除旧断言或改V1 fixture接受回归变化。服务器候选命令见deploy.md。
- 服务器 clone/backfill/publish 候选命令见 `deploy.md` 的 V2 章节；不沿用会直接写旧缓存/公开输出的 V1 默认参数。

### V2 交接与发布门禁

- 只读 schema/FX 证据、六项公式和 API 键对齐是实现的前置条件；未知历史 FX 按缺口处理，不能以临时估算跨过门禁。
- 独立 QA 未完成前，不改写 `sa-code-review.md`、`test-report.md` 或将新用例置为“通过”。
- 发布使用 GitHub-first 的已核验提交及隔离缓存；服务器不先热改再宣称同步。新版本清单只在完整月份、schema、媒体引用、回归及备份均验证后原子切换。
- 本次文档范围为requirements、sa-review、dev-plan、test-cases、sa-test-review、api-doc、deploy七份；追加授权仅允许新增`ops/opay-excellent-creatives/validate_v2_upgrade.py`及`test_v2_upgrade.py`，不修改生成器/页面等实现、生产配置或独立QA文件。
- 2026-08-27追加授权：文档稳定后由本执行者独立运行测试并审查schema2、clone/google-only/publish及默认缓存回滚，不改实现、不改`sa-code-review.md`/`test-report.md`；实际执行结果另行回报，不把本节命令模板计为通过。

### V2 独立验收脚本交付

- `validate_v2_upgrade.py --baseline-dir <冻结V1 public> --candidate-dir <staging V2>`仅读两边latest/data，必须同为2026-01—07七个成功final月，每月Meta/TikTok/Google×NG/PK六个benchmark/audit scope齐全。
- Meta/TT逐复合键比较rows、benchmarks、audits全部原字段，仅忽略对象顶层metrics；嵌套证据/媒体/未知扩展字段不能忽略。GG为JSON正整数ID、仅B、USD严格>5000、原始点击曝光交叉乘积严格高于type0基准，素材AF/安装及四项依赖指标null。
- 所有row/benchmark六项用独立Decimal公式核验，不import生成器计算函数；CPA/CPM最多6位、其余8位，null/zero区分。拒绝NaN/Infinity/溢出、重复JSON键/复合键、缺scope、混schema和校验中latest改变，输出逐月渠道数量/缺口与文件SHA。
- 脚本只读、不连接MySQL/网络；ID类型和公开证据检查不能替代数据库真实性、历史FX对账、媒体或浏览器实测。单测用临时合成public，不算真实七个月升级验收。
- 主线程负责将九个交付文件提交GitHub并在服务器运行；正式放行仍需真实七个月PASS、独立QA/媒体/CSV/移动端及发布门禁，命令见deploy.md。

## Google CPC / 图片视频 CTR 开发增量（2026-08-27）

本节替代历史 V2 的 Google B-only / Campaign B 基准计划，其余原边界保持。

| 工作项 | 范围 / 所有者 | 本次状态 |
| --- | --- | --- |
| GCP-D01 | 后端主负责人：A累计50%+严格CPC、B全PIC+VID加权CTR、证据、独立新默认缓存、全可见月policy发布门禁 | 主任务反馈完成；102项为映射协作者后续修改前结果，不当最终QA |
| GCP-D02 | 映射负责人：source6→YouTube→原type3，全部候选链/枚举/App验证 | 并行实现；本执行者不修改相关Python |
| GCP-D03 | 本执行者：report.html、validate_frontend_contract.py | 已完成，本地46/46行为用例通过；原31列CSV后追加3列 |
| GCP-D04 | 本执行者：本需求目录9份流程Markdown | 追加本日期章节；保留V1/V2历史记录 |
| GCP-D05 | 独立QA：所有候选/入选/基准/证据、V2 MetaTT全字段、旧缓存表哈希 | 使用新validate_google_cpc_upgrade.py；真实数据执行待补 |
| GCP-D06 | 发布负责人：GitHub精确SHA、一致性clone、独立回填、原子latest、回滚 | 未执行，需QA/授权门禁，不改其他report/timer |

前端只消费快照证据，不重算排名、CPC/CTR选择或改变A/B标签。`selection_policy.google.version=cpc_picvid_v1`（或明确的新证据标记）控制规则说明，历史未升级月仍按旧证据说明，避免新HTML配旧latest时误标。

已执行本地命令：`python ops\opay-excellent-creatives\validate_frontend_contract.py`，内联JS语法及46项DOM行为PASS。最终空白/范围检查见本次test-report。后续合并映射代码后由主任务/独立QA重跑后端全量，不沿用早期102项反馈。

独立验收计划：`validate_google_cpc_upgrade.py --baseline-dir <当前V2-public> --candidate-dir <新stage> --cache-db <google-cpc.sqlite3> --baseline-cache <旧V2-cache>`；再对每个真实候选月运行 `validate_frontend_contract.py --payload <month.json>`。命令中的占位路径尚未绑定/执行；不得用合成DOM夹具替代真实月验收。
