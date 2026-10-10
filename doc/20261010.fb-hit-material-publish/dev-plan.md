# 实施计划与构建

在独立 worktree/分支 codex/fb-hit-material-publish-20261010，基于e9231ab；原工作目录有其他未提交改动，未改动。

后端工作流负责 hit_material.py、service分派、精确素材读取、已有执行策略接入和针对性测试。主工作流负责列表弹窗、浏览器恢复、运行详情链接、部署脚本、文档与验证。独立SA审查后端/UI及部署。

主站与FB sidecar是不同组合版本，只部署指定文件。核心FB文件经线上读取与基线LF归一化逐个比对；发布记录页保留线上北京时间格式修复，再增加run_id直达。

检查：python -m py_compile；python -m unittest scripts.test_fb_hit_material_publish及相关FB回归；node --check static/fb-auto-publish-templates.js；node scripts/test_fb_hit_material_ui.js；node scripts/test_fb_auto_frequency_ui.js；Playwright本地浏览器走输入、确认、跳过明细、运行详情；git diff --check。服务器以相同GitHub提交重跑针对性测试。
