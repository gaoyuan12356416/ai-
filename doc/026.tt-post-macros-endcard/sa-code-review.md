# SA 代码评审

## 结论

**GO，允许进入 GitHub-first 部署和 prepare-only。** 最终集成审查 P0=0、P1=0；生产切换前仍须满足 `active/due queue=0`、`nginx -t` 通过和资产 SHA 精确匹配。

## 评审范围

- CPU：`features/tt_posts/core.py`、`service.py`、`links.py`。
- GPU：`features/tt_gpu/worker.py`。
- UI/配置：`static/tt-post-pool.html`、`deploy/tt-post*.env.example`、Nginx snippet。
- 回归：8 个 Python 测试脚本、Node bridge 与 doc/024、doc/026 合同。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P1 | `deploy/nginx-tt-short-domain-location.conf` | PCRE 中 `{18}` 未整体加引号，Nginx lexer 可能把 `}` 当块结束 | 正则整体双引号包裹，同步文档和合同测试 | 已修复并回归 |
| CR-002 | P1 | `features/tt_posts/service.py` | 真实 MySQL description 在 TT 清洗前被共享 X selector 拒绝换行/Tab | 只在 TT selector 边界折叠 whitespace，继续沿用共享安全检查 | 已修复并回归 |
| CR-003 | P1 | `features/tt_posts/service.py` | 老库字面宏 queue 精确幂等读取会因新增事实列为空而失败 | 对冻结内容完全一致的历史精确重放保留兼容，非精确重放仍冲突 | 已修复并回归 |
| CR-004 | P2 | 历史 queue 防御 | 非 terminal 老 queue 若冻结字段为空且 caption 仍是字面宏，需要运行前阻断 | 本次部署前 SQL 断言 active/due=0；新队列全部走完整冻结 | 接受，生产门禁覆盖 |

## 编译 / 验证结果

- `py_compile`、`compileall`、`git diff --check`：通过。
- Python：312/312 通过；Node bridge：53/53 assertions 通过。
- 独立 GPU 审查：三模式、资产私有快照、manifest v4、旧 v1 COS 兼容与 publish 前资产复核通过。
- 独立最终审查：P0=0、P1=0，结论 GO。
