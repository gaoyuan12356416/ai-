# 037.TT 自动发布视频制作模板选择 需求与技术设计

## 背景

TT 自动发布模板当前由服务端全局固定使用 `random_overlay`，生产 profile 为
`tt-post-random-overlay-hevc-720x1280-v3`。历史 `direct_outro`（拼接固定教程片尾）
代码与经批准片尾资产仍保留，但不能在模板编辑页选择。若直接修改全局 GPU mode，
会同时改变所有模板并使已冻结任务的 prepare/publish/reconcile 路由发生漂移。

## 目标

1. 在 TT 自动发布模板创建/编辑页增加“视频制作模板”选择。
2. 支持“随机排重”和“拼接结尾”两种固定选项。
3. 模板版本冻结选择；任务重试、发布和核对始终使用创建运行时的不可变模板版本。
4. 两种 GPU 制作服务并存，互不切换现有随机排重服务的全局配置。
5. 现有未带新字段的模板自动按“随机排重”解释，不改写历史版本或现有模板状态。

## 范围

### 包含

- 模板配置、严格校验、复制、版本哈希及 API 返回。
- 模板编辑页字段、默认值、回填、保存摘要和前端校验。
- 自动任务 prepare、creator-info、publish、reconcile 的按模板路由。
- 独立 `direct_outro` GPU service、独立 work root 和独立反向隧道。
- 单元/合同/UI 测试、生产部署、静态页验收和回滚。

### 不包含

- 删除或替换现有 `random_overlay`、`direct_outro`、`source_direct` 历史实现。
- 修改 TT 发布池的素材制作方式。
- 改写已经冻结的模板版本、运行、任务或发布账本。
- 为验收触发真实 TikTok prepare/publish canary。

## 用户故事 / 业务规则

1. 操作人在模板编辑页可选择：
   - `random_overlay`：随机排重（当前模板，无片尾、不裁尾）；
   - `direct_outro`：拼接结尾（历史模板，固定教程片尾与转场）。
2. 新模板默认 `random_overlay`。
3. 历史配置缺少 `video_template` 时，读取和执行均默认 `random_overlay`。
4. API 只接受上述两个精确值；未知值 fail closed。
5. 模板复制保留原视频制作模板；模板更新生成新版本，不修改旧版本。
6. 已创建运行继续引用原 `template_id + template_version`；后续编辑不能改变其路由。

## 交互与流程

1. 页面加载模板时回填选择；历史模板显示“随机排重”。
2. 用户选择后，保存摘要同步展示所选制作模板。
3. 保存请求写入 `video_template`；保存本身不创建运行或发布任务。
4. 运行创建后，执行器从任务引用的不可变模板版本解析路由。
5. `random_overlay` 走现有 GPU 通道；`direct_outro` 走新增 loopback 通道。

## 技术设计

### 影响模块

- `features/tt_auto_posts/validation.py`
- `features/tt_auto_posts/publisher.py`
- `features/tt_auto_posts/service.py`
- `static/tt-auto-publish-template.html`
- `static/tt-auto-publish-template.js`
- TT auto/GPU systemd units与 `.env.example`
- TT auto service、publisher、UI 和 app contract 测试

### 数据结构

模板 `config_json` 根节点新增：

```json
{"video_template":"random_overlay"}
```

无需 SQLite DDL。字段随现有不可变 `tt_auto_template_version.config_json` 保存并参与
`config_sha256`。历史 JSON 缺字段时在读取/执行边界默认 `random_overlay`，不回填数据库。

### API / 接口

- 创建/更新模板请求新增可选 `video_template`；省略时兼容为 `random_overlay`。
- 模板详情/列表中的 `config.video_template` 对新保存版本为显式值。
- 健康接口保留现有 `profile`/`source_trim_tail_seconds`，新增不含 URL/凭据的
  `video_templates` 路由摘要。
- GPU prepare 合同不新增客户端可控 mode；CPU 仍只发送固定 `expected_profile`。

### 异常与边界

- 未知制作模板：`invalid_request`，HTTP 400。
- 模板支持但执行路由未配置：`tt_auto_video_template_unavailable`，HTTP 503，
  不调用 GPU/TikTok。
- GPU 返回 profile 漂移：沿用 `tt_auto_prepared_profile_mismatch`，HTTP 409。
- `random_overlay` trim 固定 0；`direct_outro` trim 固定 4.333333 秒。
- 任务在任一重试阶段都按其模板版本重新解析同一固定路由。

## 验收标准

1. 编辑页显示两个中文选项，历史模板默认显示随机排重，保存摘要同步更新。
2. 前后端均拒绝未知值；新建/编辑/复制均保持准确配置。
3. 隔离数据库证明历史无字段模板仍走随机排重。
4. 同一任务的 prepare/publish/reconcile 使用同一路由、profile 和 trim。
5. 两个 GPU health 分别报告 `random_overlay` v3 与 `direct_outro` v2，资产身份 ready。
6. 现有随机排重 GPU PID/端口/服务不因新增 direct-outro 实例而改变。
7. 自动发布数据库 `integrity_check=ok`，部署前后模板启停、运行/任务/发布事实计数不变。
8. 生产页面浏览器验收通过；不保存测试配置、不触发真实 TikTok 发布。

## 风险与待确认

- 风险：错误复用单一 GPU URL 会在发布/核对阶段找不到对应 manifest。通过每个制作模板
  固定独立 client 与 work root 规避。
- 风险：端口冲突。部署前验证 GPU `8832` 与 CPU `18834` 未占用。
- 风险：历史 pending 任务可能由旧 profile 创建。部署门禁要求 in-flight 为 0；
  不恢复旧 SQLite 覆盖后续发布事实。
- 产品选项及默认值已由用户本次请求明确，无待确认产品决策。

## 变更记录

- 2026-08-12：创建需求；根据线上页面、生产 release `d3202fc829379fce91de6ffa4588cd29af36492e`
  和当前 health `tt-post-random-overlay-hevc-720x1280-v3` 固化设计。
- 2026-08-12：实现 commit `18559c03cc68afe83af87b963bf812e09320bb3a` 完成生产部署；
  模板 1 保持 v12/enabled 且历史 JSON 未回填。
