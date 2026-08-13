# 测试报告

## 当前结论

核心第二轮回归通过：Store 23、Publisher 21、Service 20、Runner 4、GPU 73，共 141 项；Selector 20、TT auto 主应用 10、TT Post 主应用 15、X 主应用 22、UI 15，共 82 项复合回归通过，累计 223 项。另有 Python 编译和 `git diff --check` 通过。部署后自然任务验证待完成。

## 已执行

```text
python -m py_compile ...
python scripts/test_tt_auto_post_store.py       # 23 passed
python scripts/test_tt_auto_post_publisher.py   # 21 passed
python scripts/test_tt_auto_post_service.py     # 20 passed
python scripts/test_tt_auto_post_runner.py      # 4 passed
python scripts/test_tt_gpu_worker.py            # 73 passed
$env:PYTHONPATH=(Get-Location).Path
python scripts/test_tt_posts_app_contract.py    # 15 passed
```

## 缺陷

- 第二轮中发现测试方法插入位置错误，已修正后 Service 20 项通过；未影响生产代码。
- TT Post 契约脚本直接执行时未自动加入仓库根目录，首次报 `ModuleNotFoundError`；设置仓库根 `PYTHONPATH` 后 15 项通过，未修改生产代码。
- Linux 候选 release 首轮 runner 测试发现测试夹具使用当前目录锁路径，不符合生产只允许 `/run/tt-auto-post/` 的校验；已改为按操作系统选择测试锁目录，生产代码不变。

## 发布建议

完成主应用 TT/X 契约后允许发布到空窗；不得用真实 TikTok 发帖验证。
