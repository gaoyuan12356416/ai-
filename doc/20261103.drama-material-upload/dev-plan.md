# 实施计划

新增 features/drama_synthesis/material_source.py 和独立systemd worker，复用已测试的共享入库契约。主API仅插入两个路由和任务DTO装饰；页面列表、详情新增上传动作并纳入现有10秒轮询。

生产为组合版本，用 scripts/patch_drama_material_source.py 对实测快照做局部替换。脚本校验部署前SHA与共享依赖，不覆盖整份Git app.py。新代码先提交GitHub，再服务器精确checkout部署。验证包含 Python编译、23项功能测试、16项共享回归、JS语法与浏览器模拟验收。
