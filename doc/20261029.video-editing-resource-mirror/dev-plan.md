# 开发与验证计划

1. 从d043f80建立独立分支，保留原工作目录。
2. 并行完成目录/上传/入库、GPU镜像、前端；主工作流完成v2草稿、单元任务和路由整合。
3. 生产漂移复核：CPU compact分支c0ddfa9、GPU05f48e2的导出profile合入；GPU发布仅含recipe/render/gpu_integration和部署说明。
4. 运行python -m unittest discover -s tests -p "test_video_editing_*.py"、node --check及现有JS模型/浏览器契约测试；使用真实浏览器验证页面。
5. GitHub提交推送；按部署脚本check/prepare/apply/restore分阶段上线，GPU先于CPU；记录证据和可兼容回退方法。

验收结果仅在实际运行后记入test-report.md。
