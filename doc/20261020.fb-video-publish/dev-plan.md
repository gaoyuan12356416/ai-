# 开发计划

1. 基线：41b5ca4 新建独立 codex/fb-video-publish-20261020；当前主 API/nav只读下载比对。
2. 并行：core/service/worker与旧FB门禁；sources/templates/links；三正式页面和UI测试。
3. 主 API 独立 gateway/client：Cookie、导航、同源、4 MiB、trusted actor；main app
   使用 AST/唯一锚点补丁，部署时 SHA 校验，禁止旧整包覆盖。
4. 本地 SQLite+fake Graph/GPU/links 状态机、并发/失败/CAS/配额测试；真实 DTO UI 和响应式检查。
5. SA 代码评审和修复回归；需求、接口、测试、部署及 bug 文件保存实际结果。
6. 提交推送 GitHub，CPU取精确提交，数据盘备份和迁移演练，按白名单部署。
7. 只读线上验收和隔离 prepare-only GPU；维护技能记录正式运行合同。

编译命令：`python -m compileall -q features/fb_video_publish`，
修改旧 FB 模块与 main staging文件逐个 `python -m py_compile`，
JS 用 `node --check`。单元测试入口统一 `scripts/test_fb_video_publish_*.py`；
浏览器测试入口由 UI 实现记录到 test-report。所有测试禁止真实 Graph 发布。

## 文件归属

core/service/worker/guards、旧FB改动和独立worker：backend代理；
sources/templates/links：source代理；新三页面和专用js/css：UI代理；
主 API gateway/client、导航集成脚本、部署及文档：root。各自不覆盖其他文件。
