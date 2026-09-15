# 开发计划

隔离工作区D:/codex/worktrees/meta-video-delete-continue-20260915；分支codex/meta-video-delete-continue-20260915；基线2581cdf，当前运行d4953ad。

先增加三个独立Video回归：旧代码三项均失败。Service改为states.issubset(TERMINAL_SUCCESS)，避免set与tuple运算。Graph独立实现无效ID后的同凭证完整读取，保留失败证据。新真实Graph/Service/Store集成验证回执和重试。

不改表结构或前端。检查命令：python -m unittest discover -s tests -p 'test_fb_ad_asset_delete_*.py' -q；py_compile两个模块和新部署脚本；git diff --check。

GitHub先提交、服务器精确检出与293项回归，再备份和仅切换Graph/Service两个模块，排空发布service并窄重启主API。预览参数保存、部署后按原所有者重新预览，验收不调用Meta DELETE。
