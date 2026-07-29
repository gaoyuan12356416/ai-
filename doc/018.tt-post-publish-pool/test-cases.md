# 测试用例

## 测试范围

CPU 状态机、账号安全边界、GPU 成片、发布门禁、主后台权限、页面交互、部署单元和线上关闭态健康检查。

## 测试数据

- 临时 SQLite。
- 伪造的安全账号行和占位 Token；测试不得使用真实 Token。
- 本地短视频与固定新版片尾测试副本。
- Mock TikTok creator info/init/status 响应。
- 北京时间边界、并发 claim 和 unknown outcome 数据。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 账号列表安全 DTO | 快照含 Token | 查询列表 | 响应/日志不含 Token | P0 | 待执行 |
| TC-002 | 不合格账号 | Token 过期或禁发 | 创建任务 | 返回 `account_not_eligible` | P0 | 待执行 |
| TC-003 | creator info 无 scope | Mock `scope_not_authorized` | 预检 | fail-close，不显示可发 | P0 | 待执行 |
| TC-004 | 素材解析 | 有效素材 ID | 预览 | 返回真实 `content_id` 和媒体 | P0 | 待执行 |
| TC-005 | 固定描述模板 | 有效 `content_id` | 冻结任务 | 固定全文逐字落库，`{{contect_id}}` 被真实 ID 替换 | P0 | 待执行 |
| TC-006 | 篡改固定描述 | 保留正确 Drama ID、修改其他文案 | 创建任务 | `tt_caption_fixed_template_mismatch`，GPU 不开始制作 | P0 | 待执行 |
| TC-007 | 隐私无默认 | creator info 成功 | 打开页面 | 未手选时不可提交 | P0 | 待执行 |
| TC-008 | 互动无默认 | creator info 成功 | 打开页面 | 三项均未勾选，禁用项灰显 | P0 | 待执行 |
| TC-009 | 显式同意 | 未勾同意 | 创建任务 | `consent_required` | P0 | 待执行 |
| TC-010 | 时间转换 | 上海时间 | 创建任务 | UTC 落库正确 | P0 | 待执行 |
| TC-011 | 素材全局去重 | 素材已有有效任务 | 并发创建 | 仅一个成功 | P0 | 待执行 |
| TC-012 | 账号时间冲突 | 同账号同一时点 | 并发创建 | 仅一个成功 | P0 | 待执行 |
| TC-013 | 原子 claim | 多 runner | 同时领取 | 单任务只被一个 runner 领取 | P0 | 待执行 |
| TC-014 | 超时不补发 | 超过宽限期 | 执行 runner | 标记 `missed` | P0 | 待执行 |
| TC-015 | init 前取消 | scheduled | 取消 | 成功释放素材 | P0 | 待执行 |
| TC-016 | init 后取消 | 已有 publish_id | 取消 | 拒绝，只允许对账 | P0 | 待执行 |
| TC-017 | unknown 禁重发 | init 结果不明 | runner 重跑 | 不再 init，状态 `needs_review` | P0 | 待执行 |
| TC-018 | 三重门禁关闭 | 任一 gate=0 | 到点执行 | 不调用 init，`blocked_compliance` | P0 | 待执行 |
| TC-019 | GPU 数据盘 | GPU worker | prepare | 只在 `/data/tt-post-publisher` 写工作/成片 | P0 | 待执行 |
| TC-020 | GPU 成片 | 正片+新版片尾 | prepare | 音视频连续，SHA/大小/时长正确 | P0 | 待执行 |
| TC-021 | 动态 Drama ID | 固定片尾含示例 ID | prepare | 真实 ID 清晰，示例被标注教程 | P1 | 待执行 |
| TC-022 | GPU 仅 loopback | 服务启动 | 检查监听 | 仅 `127.0.0.1` | P0 | 待执行 |
| TC-023 | Token 不落盘 | creator/publish mock | 全流程搜索产物和日志 | 无 Token/Authorization | P0 | 待执行 |
| TC-024 | 已有 publish_id 对账 | status mock | reconcile | 只调用 status，不再 init | P0 | 待执行 |
| TC-025 | 页面权限 | 无权限用户 | 打开页面/API | 403 或权限提示 | P0 | 待执行 |
| TC-026 | X 回归 | 现有 X 测试 | 执行回归 | X 发布池无回归 | P0 | 待执行 |
| TC-027 | 去除原 CTA | 39.1 秒样例素材 | 默认 prepare | 去尾 4.333333 秒，旧 CTA 不进入成片 | P0 | 待执行 |
| TC-028 | Logo 和 phone-match | Logo/片尾文件有效 | prepare | 正片 Logo 正确，0.9 秒缩屏叠底过渡，无硬切 | P0 | 待执行 |
| TC-029 | GPU 自行冻结源指纹 | CPU 仅提供 URL | prepare | 下载、SHA和大小均在 `/data` 产生，CPU 不重复下载 | P0 | 待执行 |

## 回归范围

- X 素材池、短剧池、多时点、账号权限和日志。
- AI 平台登录、导航、素材状态和其他现有 API。
- CPU `socialkit-tiktok-account-sync` 保持只读消费边界。
- GPU 既有 `x-post-media-repair` 服务和 18820 隧道。
