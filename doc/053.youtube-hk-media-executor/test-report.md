# 测试报告

开发阶段运行 `python -m unittest scripts.test_drama_youtube_hk_media scripts.test_drama_youtube_canary scripts.test_drama_synthesis_upgrade -v`，150 项通过；py_compile 与 diff-check 通过。未调用真实 YouTube 上传、评论或创建发布任务。生产结果在切换后补充。
