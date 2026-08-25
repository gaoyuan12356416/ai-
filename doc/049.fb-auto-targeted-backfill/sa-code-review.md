# SA 代码评审

## 结论

通过，未发现未关闭的 P0/P1 问题。实现保持最小扩展，不新增 Graph 调用或 HTTP 暴露面。

## 评审范围

- `features/fb_auto_posts/core.py`
- `scripts/fb_auto_post_targeted_backfill.py`
- `scripts/test_fb_auto_store.py`
- `scripts/test_fb_auto_post_targeted_backfill.py`
- `doc/049.fb-auto-targeted-backfill/*`

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | 高 | `core.py/_only_target_pages` | 字典过滤会静默折叠异常重复 Page | 显式比较选中行数与唯一 Page 数并拒绝 | 已修复 |
| CR-002 | 高 | CLI main | 发布/预制门禁关闭时仍可能建单形成长期积压 | build runtime 后先验证两道 live gate | 已修复 |
| CR-003 | 高 | 来源验证 | 仅比较 template version 未核对 run 冻结 config | 联表读取当前不可变版本 config 并要求字节一致 | 已修复 |
| CR-004 | 中 | 报告写入 | 直接 replace 可能覆盖并缺少完整落盘保证 | 临时文件 fsync + 新目标 hard-link + 目录 fsync | 已修复 |
| CR-005 | 中 | Windows 本地测试 | 目录 fd fsync 在 Windows 报权限错误 | 目录 fsync 仅在 Linux 执行，生产语义不变 | 已修复 |

## 编译 / 验证结果

```text
python -m py_compile ...                         PASS
python -m unittest ...targeted... ...store      52/52 PASS
python -m unittest discover ...test_fb_auto...  141/141 PASS
git diff --check                                PASS（仅既有换行提示）
```
