# 普通视频标题跟随频道语言

标题生成从当前租户保存的频道模板读取语言，不接受请求中的语言覆盖。频道未设置语言时使用可识别的素材语言，否则用英语，并在弹窗说明回退。保留本条成片和全剧推广两种事实来源。

API 保留 `titles` 字符串数组，新增按相同索引对应的 `translations_zh`，以及实际使用的 `language`、`language_label` 和 `language_source`。译文缺失时保留可用标题并提示；手填和发布流程不受影响。译文仅用于核对，采用和复制只取原语言标题。切换频道会清除旧候选及已采用且未经手改的标题；过期生成响应不覆盖新频道。

验证：`python -m unittest discover -s tests -p test_youtube_regular_workflow.py -q`；`node tests/test_youtube_regular_titles_ui.js`；修改文件的 Python/JS 语法检查与 `git diff --check`。

CPU 从 GitHub fetch 本提交并建立独立 checkout 后，执行 `python3 ops/youtube-channel-titles-20261120/deploy.py install`。安装器校验已部署的普通视频基线，备份两份静态文件及两个 Python 模块，仅通过现有安全排空机制重启主 API，按切换前快照检查恢复 X/YouTube 调度。YouTube 准备与 canonical worker 不切换，无数据库或任务变更。

回滚在同一 checkout 执行 `python3 ops/youtube-channel-titles-20261120/deploy.py rollback`。备份及版本记录在 `/mnt/data-disk/youtube-channel-titles-20261120/<commit>/`，只恢复本次代码，保留当前数据库、账户语言、队列及发布回执。发现后续文件变化时先合并回退差异。
