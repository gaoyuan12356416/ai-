# SA 代码评审

## 结论

待评审。当前仅有需求、API、开发、测试和部署文档；业务代码尚未由本评审确认，不能给出“通过”或发布建议。

## 计划评审范围

- `features/tt_posts/core.py`
- `features/tt_posts/code_routes.py`
- `features/tt_posts/links.py`
- `features/tt_posts/service.py`
- `app.py`
- `static/tt-drama-code-search.html`
- `static/tt-drama-code-search.js`
- `deploy/nginx/tt-drama-code-search.conf`
- `deploy/tt-post*.env.example` 及 Redis/systemd 资产
- 新增/修改的 `scripts/test_tt_*`

## 必查清单

| 编号 | 严重级别 | 检查项 | 通过条件 | 状态 |
| --- | --- | --- | --- | --- |
| CR-01 | P0 | 原 `/tt` 隔离 | 原 HTML/JS 无 diff，新 route 不抢占旧 route | 待评审 |
| CR-02 | P0 | schema 加法迁移 | 幂等建表/索引，不重建旧表，不改历史行 | 待评审 |
| CR-03 | P0 | code DB 约束 | 大写四位、PK、queue unique、状态与 channel 约束完整 | 待评审 |
| CR-04 | P0 | 分配事务 | queue 幂等检查、code 分配、route、caption 位于同一 `BEGIN IMMEDIATE` | 待评审 |
| CR-05 | P0 | 碰撞处理 | 普通 INSERT + PK 冲突重试，不使用 `INSERT OR REPLACE` | 待评审 |
| CR-06 | P0 | 全容量判断 | 只有精确全满才回收；最早排序确定；有审计 | 待评审 |
| CR-07 | P0 | `{code}` tokenizer | 精确 token、一次非递归、preview 不消耗、UTF-16 限制 | 待评审 |
| CR-08 | P0 | 发布幂等/unknown | 同 queue 重试复用 code，不因未知结果二次分配 | 待评审 |
| CR-09 | P0 | 正式 URL | `c` 尾部 queue ID、channel TT、字段映射和标准编码正确 | 待评审 |
| CR-10 | P0 | 最新 published | `published_at DESC, queue_id DESC`，clone 只改 channel且不写库 | 待评审 |
| CR-11 | P0 | generic fallback | `af_dp,c=TTpost,af_c_id=0001,af_channel=source`，不伪造其他字段 | 待评审 |
| CR-12 | P0 | Redis 事实边界 | DB commit 不依赖 Redis；任何缓存故障回退 SQLite | 待评审 |
| CR-13 | P0 | 陈旧缓存 | 回收后绝不返回旧值；`DEL`/覆盖失败时旋转 namespace 并旁路 | 待评审 |
| CR-14 | P0 | 公共输入/输出 | exact GET、参数唯一、source 枚举、无 secret/内部异常泄漏 | 待评审 |
| CR-15 | P0 | target 校验 | HTTPS、精确 host/path、无端口/userinfo、af_dp 一致 | 待评审 |
| CR-16 | P0 | 页面导航 | 只有 resolver 成功且 URL 校验通过才设置 CTA/导航 | 待评审 |
| CR-17 | P0 | 横滑不误触 | pointer/touch/mouse 状态机有位移阈值、取消与清理 | 待评审 |
| CR-18 | P1 | 五条完整性 | 动态或 fallback 都恰好五条，不混成任意数量 | 待评审 |
| CR-19 | P1 | 可访问性 | 按钮 aria、键盘、焦点、首尾禁用、reduced motion | 待评审 |
| CR-20 | P0 | 测试安全 | 全部 fake/临时数据，无 publish/canary/run-now 调用 | 待评审 |

## 待填写问题清单

代码可用后逐条填写，格式如下；不得提前写“无问题”：

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 待补录 | - | - | 尚未开始代码评审 | 完成实现后评审 exact diff | Open |

## 编译与验证结果

待代码完成后补录实际命令、退出码、断言数量和失败项。当前无业务代码验证结果。

## 发布门禁

存在任一 P0/P1 open finding、自动化未执行、真实浏览器手势未验证、Redis 陈旧缓存未覆盖或原 `/tt` 出现 diff 时，结论必须保持“不通过”。
