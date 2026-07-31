# 024.tt-post-url-macro 需求与技术设计

## 背景

TikTok 测试 Post 的描述模板在数据库和 CPU 请求中保存了空行，但 TikTok 客户端的紧凑展示将其折叠。新的描述还需要支持 `{url}`，将指定 Dramawave W2A 地址按 X 渠道规则补齐归因参数，再输出 `gy.g2flow.com` 短链。

## 目标

- 描述模板、CPU 到 GPU 请求、GPU 到 TikTok 初始化参数全链路保留真实换行字符。
- 新增可选的精确宏 `{url}`。
- `{url}` 替换为 `https://gy.g2flow.com/s2l/<id>.html`。
- 短链跳转到 `https://www.dramawavew2a.com/ads/101/2250/view`，参数格式及顺序与 X 渠道一致。
- 不含 `{url}` 的历史模板、队列和发布流程保持不变。

## 范围

### 包含

- TT 发布池页面模板校验与长度预览。
- 素材入池、每日/手动队列冻结、队列幂等重放。
- W2A 参数构造、短链编号、不可变跳转页落盘。
- CPU、GPU 与 TikTok 初始化参数的换行回归测试。
- Nginx 的 TT 短链独立路由样例。

### 不包含

- 不修改 X 历史短链或 X 发布账本。
- 不使用不可见字符模拟换行。
- 不承诺 TikTok App 在所有视图中按原始换行排版。
- 本分支不部署生产、不修改发布开关、不发送真实 Post。

## 用户故事 / 业务规则

1. 模板仍必须含 `{{contect_id}}` 或 `{{content_id}}`。
2. `{url}` 为可选、区分大小写、单层花括号宏；未知宏一律拒绝。
3. `{url}` 存在时，素材名、剧名、语言、标签、账号 ID/名称/用户名必须完整。
4. 短链编号使用 `8_000_000_000_000_000_000 + TT 素材池 ID`，公开路径固定为 19 位且以 `8` 开头，避免与 X 文件冲突。
5. W2A 参数顺序固定为 `c, af_adset, af_adset_id, af_ad, af_ad_id, af_channel, af_c_id, af_dp`。
6. `af_channel=AIpost`，`af_c_id=TT 队列 ID`，`af_dp=content_id`。
7. 模板在队列创建时冻结短链；真实长链和跳转页在队列已领取、TikTok 初始化前生成。
8. 跳转页不可变；同一编号写入同一目标可重放，目标不同则失败关闭。

## 交互与流程

1. 页面校验模板并用最长 TT 短链样例计算 2200 UTF-16 单位限制。
2. 素材入池时保留 `{url}`，同时冻结归因所需素材元数据。
3. 创建发布队列时生成短链编号和公开短链，并替换描述中的 `{url}`。
4. Worker 领取队列并复核 Creator 能力。
5. CPU 构造并持久化完整 W2A 长链，原子创建短链跳转页。
6. 上述步骤成功后才允许进入 TikTok Direct Post 初始化。

## 技术设计

### 影响模块

- `features/tt_posts/core.py`：宏渲染、元数据及短链字段冻结。
- `features/tt_posts/links.py`：W2A、短链验证及不可变跳转页。
- `features/tt_posts/service.py`：素材元数据传递、发布前准备。
- `static/tt-post-pool.html`：前端校验及长度预览。
- `deploy/nginx-tt-short-domain-location.conf`：TT 短链独立静态目录。

### 数据结构

`tt_post_queue` 新增 `material_name`、`drama_name`、`material_language`、`material_tag`、`short_link_id`、`short_url`、`long_url`。素材入池和循环池新增对应素材元数据字段。迁移为可重复执行的增量列与短链唯一索引。

### API / 接口

现有 `caption_template` 字段支持 `{url}`，不新增客户端必填字段。队列 DTO 返回冻结后的 `caption_text`、`short_url`、`short_link_id` 和准备完成后的 `long_url`。

### 异常与边界

- 缺少归因元数据：`tt_post_link_metadata_incomplete`。
- `{url}` 未绑定有效 TT 短链：`caption_url_required`。
- 跳转目标不合法：`tt_short_link_target_invalid`。
- 跳转页目标冲突：`tt_short_link_conflict`。
- 文件系统不可写：`tt_short_link_write_failed`。
- 任一短链准备错误均发生在 TikTok 初始化前，并记录为未创建远端 Post 的已知失败。

## 验收标准

- 带双空行的描述从模板到 TikTok `post_info.title` 字符串完全一致。
- `{url}` 精确替换为 `https://gy.g2flow.com/s2l/8xxxxxxxxxxxxxxxxxx.html`。
- 长链基址及八个查询参数顺序、值符合 X 归因契约。
- 无 `{url}` 的全部旧测试通过。
- 同一短链不能被并发或重试覆盖为不同目标。
- Nginx TT 规则置于 X 通用数字规则之前，且使用独立目录。

## 风险与待确认

- TikTok 客户端可能在紧凑视图折叠换行；后台只能证明提交值未被改写。
- 正式上线需要在 `gy.g2flow.com` TLS Server 中添加 TT 专用 location，并先完成 `nginx -t`。
- 正式 Post 验证应由主任务在用户明确授权后执行。

## 变更记录

- 2026-07-31：完成设计、实现及离线回归；未部署生产。
