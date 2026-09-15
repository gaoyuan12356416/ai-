# 开发计划

隔离分支 codex/meta-video-direct-delete-20260915；本地基于7a757cee，生产代码基线00c887238010。

1. 后端实现 Video 独立 Token 选择/直接 DELETE；预览与执行排除 Video 读取门禁；台账兼容旧blocked并保留审计。
2. 前端同步计数/筛选/现有确认，无额外参数或开关。
3. QA 独立补充 Video 测试与代码审查；维护原 Creative/Ad 回归。
4. 补齐规范文档，GitHub提交推送，服务端精确检出后备份部署，仅重启主API。

验证：python -m unittest discover -s tests -p test_fb_ad_asset_delete_*.py -q；Python py_compile五个修改模块及deploy_meta_video_direct.py；node --check static/fb-post-ad-delete.js；git diff --check。

本机没有pytest，使用项目既有unittest runner，不引入依赖。
