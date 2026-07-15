# 测试报告

## 测试结论

本地与生产同源补丁测试通过；线上数据库、服务与API回归待部署后补录。数据库通用端点 `@@read_only=1`，当前构成发布阻塞，现网代码尚未变更。

## 测试范围

执行批次、限流熔断、Graph安全门、续跑状态、MySQL日志适配、迁移保护、日志UI、静态缓存与共享app窄补丁。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Python unittest | 21 | 21 | 0 | 0 |
| Python compile | 4 | 4 | 0 | 0 |
| JavaScript syntax | 1 | 1 | 0 | 0 |
| 生产同源补丁演练 | 3 | 3 | 0 | 0 |
| 线上回归 | 5 | 0 | 0 | 5（等待ads_ai写端点） |

## 缺陷情况

BUG-001 及评审发现均已修复；未发现未关闭的 P0/P1。

## 验证证据

- `python -m unittest discover -s tests -p 'test_*.py' -v`：21/21。
- 4个Python文件 `py_compile`：通过。
- `node --check static/ad-control-pages.js`：通过。
- 生产同源复合app临时快照：第一次patch=changed、第二次=unchanged、编译通过、所需函数齐全。
- 7个HTML：CSS/JS全部引用 `20260715log1`。
- 线上只读基线：旧 `ad_control_rule` 为0；唯一启用的是新规则组。
- 线上建表尝试：账号grant包含ads_ai DDL/DML，但节点返回 MySQL 1290 read-only；表未创建，代码未落盘。

## 遗留风险

- 正式Meta pause不作为上线首测；先做日志读、preview/dry-run。
- `results_json` 后续需制定180天归档策略。
- 共享生产app部署时仍必须对真实文件做备份和diff门禁。

## 发布建议

获得 `ads_ai` 写端点（或 DBA 建表并提供可写连接）后按 `deploy.md` 继续；只有建表回读、回填幂等、服务active和API回归全部通过后才宣布完成。
