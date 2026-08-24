# 测试报告

## 测试结论

本地测试与生产只读数据核对通过；等待生产 release 部署验收。

## 测试范围

见 `test-cases.md`。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Repository + publisher 定向 | 24 | 24 | 0 | 0 |
| FB 自动发布完整回归 | 129 | 129 | 0 | 0 |
| X/TT 合并基线 | 66 | 66 | 0 | 0 |
| 生产部署验收 | 4 | 1 | 0 | 3 |

## 缺陷情况

尚无确认缺陷。

## 验证证据

- `python -m py_compile features/fb_auto_posts/repositories.py features/fb_auto_posts/publisher.py`：通过。
- `python -m unittest scripts.test_fb_auto_repositories scripts.test_fb_auto_publisher`：24 项通过。
- `python -m unittest discover -s scripts -p "test_fb_auto*.py"`：129 项通过。
- X/TT 四个 contract 测试模块：66 项通过。
- 生产只读字段核对：`status` 为 NOT NULL、默认 0；总计 21,210 行，NULL=0。
- 生产组 62：总 Page 13、旧口径可发 8、新口径可发 12、被封 1。

## 遗留风险

- 既有 run 17~21 是冻结审计快照，仍保持 8 可发/5 跳过；不做历史回填。
- 发布验证不创建真实 Meta Post；以 release 测试、只读 Page 池、health、日志和状态机不变量验收。

## 发布建议

本地质量门已通过，建议按 `deploy.md` 先备份再发布精确 GitHub SHA。
