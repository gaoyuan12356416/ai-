# 测试用例

## 测试范围

模板校验/版本兼容、执行三阶段路由、服务 health、页面回填/请求体/摘要、部署单元、
CPU/GPU 生产状态和浏览器可见行为。

## 测试数据

- 隔离临时 SQLite：一个显式 `random_overlay` 模板、一个显式 `direct_outro` 模板、
  一个缺少 `video_template` 的历史模板。
- Fake GPU clients 分别标记 random/direct 调用，不访问外网。
- 生产仅读取现有模板 1，不保存、不手动执行。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-01 | 新模板默认 | 请求省略字段 | normalize | 输出 `random_overlay` | P0 | 通过 |
| TC-02 | 两个合法枚举 | 分别传两个值 | normalize | 精确保留 | P0 | 通过 |
| TC-03 | 未知枚举 | 传任意第三值 | normalize | HTTP/错误码 fail closed | P0 | 通过 |
| TC-04 | 历史配置执行 | 版本 JSON 缺字段 | prepare | 使用 random client/profile/trim=0 | P0 | 通过 |
| TC-05 | direct prepare | 模板为 direct | prepare | 使用 direct client/v2/trim=4.333333 | P0 | 通过 |
| TC-06 | direct publish/reconcile | direct 任务已 ready/有 publish_id | 执行两阶段 | 始终使用 direct client | P0 | 通过 |
| TC-07 | 路由缺失 | direct 模板但 executor 未配置 direct route | 执行 | 503 且两 client 均未调用 | P0 | 通过 |
| TC-08 | GPU profile 漂移 | Fake GPU 返回错误 profile | prepare | 409，不进入 ready | P0 | 通过 |
| TC-09 | 复制/新版本 | 复制并编辑模板 | 读历史/新版本 | 选择准确、旧版本不变 | P1 | 通过 |
| TC-10 | 页面默认回填 | 历史模板无字段 | 打开编辑页 | 选中“随机排重” | P0 | 静态通过，生产待验收 |
| TC-11 | 页面选择/请求体 | 选择拼接结尾 | 构造保存请求 | 含 `video_template=direct_outro`，摘要同步 | P0 | 通过 |
| TC-12 | 前端未知值 | 注入未知配置 | hydrate/build | 回退或拒绝，不能静默提交未知值 | P1 | 通过 |
| TC-13 | health 合同 | 双路由配置 | GET `/health` | 两 route/profile/trim 可见，无 URL/凭据 | P1 | 通过 |
| TC-14 | systemd 静态合同 | 新 unit/env 示例 | 解析文件 | 8832→18834、独立 work root、最小权限 | P1 | 通过 |
| TC-15 | 生产 GPU health | 两服务已启动 | 调用两个 loopback health | mode/profile/asset ready 准确 | P0 | 待执行 |
| TC-16 | 生产无副作用 | 部署前后 | 对比 DB/PID/ledger | 无新 run/task/publish，原 random PID 不变 | P0 | 待执行 |
| TC-17 | 真实浏览器 | 登录态 Chrome | 打开模板 1 编辑页 | 两选项可见，默认随机，未保存 | P0 | 待执行 |

## 回归范围

TT auto 全量测试；TT GPU worker 与 random-overlay 回归；主 API TT auto app contract；
静态 JS 语法；不运行真实发布 runner/canary。
