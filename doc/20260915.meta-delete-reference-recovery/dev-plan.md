# 实施计划

1. 隔离工作区与 codex/meta-delete-reference-recovery-20260915 分支，基线 e18d88f。
2. 并行 Graph 诊断、UI 重核验入口；主线程实现完整视频引用索引与持久化核验互斥。
3. 核验测试：python -m unittest discover -s tests -p 'test_fb_ad_asset_delete_*.py' -q；node --check static/fb-post-ad-delete.js；Python 编译；git diff --check。
4. GitHub 提交后服务端检出精确提交，运行只读 reference probe，记录耗时/行数/文件体积/内存。
5. 备份代码、双份静态和在线任务 SQLite，暂停相关 timer 并排空发布任务后，仅重启主 API。
6. 公网401/静态哈希/页面按钮验证；原任务只读重核验不发 DELETE。受限账户需要管理员处理。
