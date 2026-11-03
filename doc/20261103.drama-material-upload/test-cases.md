# 测试用例

- 完成状态、视频类型、服务端身份与固定3214映射。
- 本人/管理员允许，非本人拒绝；未登录、API token、跨域拒绝。
- 相同产物16次并发、服务重启、两worker租约只提交一次；多产物相互独立。
- 真正成功需正式行及接收回执绑定；缺回执、字段冲突、超时、提交中断不重发。
- 任务删除/输出URL/ETag变化阻止错误视频入库。
- 客户端URL/产品/人员不可覆盖；旧路由保持可执行。
- 已完成按钮、成功ID、等待/失败提示、文本转义、多产物选择、取消、重复点击。
- 已完成合成中的入库活动触发现有10秒轮询；成功/失败停止，保留页面可见性与权限判断。

执行：python -m unittest tests.test_drama_material_source tests.test_video_editing_material_source -q；node --test tests/test_drama_material_upload_ui.js。
