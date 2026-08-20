# SA 代码评审

## 结论

代码评审与本地完整回归通过；待生产只读验收后关闭发布项。

## 评审范围

- 两张 FB 模板页面、公共 CSS/JS、列表与表单脚本。
- `scripts/test_fb_auto_app_contract.py`。
- 未涉及后端 API、数据库或发布执行代码。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-01 | P1 | 公共确认框 | 多次打开时旧 `cancel` 监听可能残留 | 关闭时显式移除监听 | 已修复 |
| CR-02 | P1 | 表单详情 | 详情必须在 Page 池渲染后回填选中状态 | 先加载组，再加载详情并二次渲染 | 已修复 |
| CR-03 | P1 | 列表写操作 | 必须保留版本并发控制和 run-now 幂等键 | 保留 `expected_version` / `operation_id` | 已确认 |
| CR-04 | P2 | 输出编码 | Page/模板字段进入 innerHTML | 所有动态文本经 `escapeHtml` | 已确认 |

## 编译 / 验证结果

- 三个新增 JavaScript：`node --check` 通过。
- `python -m unittest scripts.test_fb_auto_app_contract`：5/5 通过。
- FB 全量回归 80/80、X/TT 主契约 66/66 通过。
- Playwright 11 个本地 mock 场景通过，控制台 0 error/0 warning。
