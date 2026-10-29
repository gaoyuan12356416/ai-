# 实施计划

基线：`4c7cd30878ad46dd2908839a60dc09642ab2eda9`。分支：`codex/meta-cover-pipeline-20261029`。

1. 核对版本、停机原因、未决任务与账本。
2. 实现滑动调度、有界新鲜来源准备、独立清理循环和共享限流。
3. 验证恢复/停止顺序、未知保护、性能标记及报表兼容。
4. 并行设计用例与代码评审，实际缺陷逐项记录到 bugs/。
5. 运行编译、针对性测试、全模块回归与 diff 检查。
6. 提交并推送 GitHub；服务器备份并拉取确切版本，窄范围切换服务。
7. 验证实际广告持续出队、清理回执、预算与 lane 指标，记录样本与限制。

在 server-monitors/meta-violation-material-cleanup 执行：

```powershell
python -m compileall -q cover.py meta_violation_cleanup tests
python -m unittest discover -s tests -p 'test_cover*.py'
python -m unittest discover -s tests
git diff --check
```

Linux 专属 flock/信号场景须在 Linux 验证；不能以静态检查代替。结果填入 test-report.md。