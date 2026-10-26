# 部署与回退

基线c7489e1（运行文件等同已部署e59fd0b）。沿用43.166.187.96主API、FB runtime及静态站点，GitHub-first。

scripts/deploy_fb_group_page_languages.py限定变更白名单、逐目标SHA预检。停止新组调度后排空原生产者和租约，备份现库/文件，在副本排练迁移；实际迁移比较既有fb_video/fb_group表全部旧列的行数与哈希，保留现计划和任务，窄重启并恢复全部原状态。部署前后避开已启用新旧池时点。

功能回退只关闭新组池写入/调度；保留兼容v1/v2执行器、现库、已接受任务与短链。准确release、manifest、回退命令在上线验收后补充。
