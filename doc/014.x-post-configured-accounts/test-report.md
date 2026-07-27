# 014.x-post-configured-accounts 测试报告

## 测试结论

离线测试通过，可以按 GitHub-first、备份、精确版本和零补发门禁部署。首个真实 9 账号批次只允许由下一次自然 timer 触发。

## 测试范围

- 账号配置 1/3/9/50/51 边界、唯一性和顺序。
- 9 账号全批预检、原子计划、顺序发布和动态汇总。
- 同日历史 3 queue 在扩容后原样恢复且不选材、不补建。
- 旧 3 账号无 queue 的失败批次与 9 账号配置冲突退出。
- dynamic `expected_count` 失败审计与 client/sidecar/store 透传。
- daily bearer 精确账号范围、请求体上限和响应上限。
- DTO 严格布尔、账号列表 12 列、发布日志动态分母。
- 原素材池 FIFO、排重、GPU 修复、unknown、OAuth 和 ledger 回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | ---: | ---: | ---: | ---: |
| X 全套单元/集成/静态契约 | 197 | 197 | 0 | 0 |
| Python 编译 | 4 | 4 | 0 | 0 |
| 页面 JavaScript 解析 | 2 | 2 | 0 | 0 |
| diff 格式检查 | 1 | 1 | 0 | 0 |

## 缺陷情况

评审和回归中发现的固定数量、失败字段丢失、账号顺序、请求体和超时预算问题均已修复并回归；无未解决缺陷，因此未保留占位 BUG 文件。

## 验证证据

```text
python -X utf8 -m unittest discover -s scripts -p "test_x*.py"
Ran 197 tests
OK

python -m py_compile features/x_accounts/client.py \
  features/x_accounts/oauth_service.py \
  features/x_posts/service.py scripts/x_post_daily_runner.py

HTML inline JavaScript parse: OK
git diff --check: OK
```

## 遗留风险

- 生产素材池当前仅能支撑有限天数；每天需至少补充 9 条互异可用素材。
- 50 账号是配置和数据合同上限，不承诺当前 360 分钟 unit 能覆盖 50 账号最坏执行时间；本次生产范围仅 9。
- 首个真实 9 账号自然批次尚未发生，需在 2026-07-28 10:00 后核对 9 条 queue/log/Post 与实际耗时。

## 发布建议

通过。部署当天不得手工启动 daily service；更新两份 env、release、static 和 unit 后，只验证配置、页面、服务、计数和下一触发时间。
