# 代码复核

上传代理进行了状态机独立复核，已修复并回归 HTTP200缺Location 与 unknown 后403导致重复上传风险。首次source hash/size冻结，approved cover读取一次并使用相同bytes上传，旧worker排除reviewed workflow。统一记录只增加受校验的 provenance JSON，不改变现有 MySQL 列。

主实现复核：素材请求受服务器只读SELECT、产品/域名和容量限制；浏览器不能提交任意SQL/频道凭据；资产鉴权、摘要和版本绑定；通知状态持久化；纯AI生图隔离工作目录并移除应用密钥环境；默认文案提交后冻结。管理员手动替代封面为任务所有者建立不可变副本，避免跨owner访问失败。

待发布前确认：主API精确文件与当前生产基线一致、旧worker加载新claim、数据盘挂载、GitHub提交可读取、全部离线和权限检查通过。实际证据记录在 test-report.md/deploy.md。

## 2026-09-10 加载修复独立复核

后端轻量bootstrap和独立channels保留原Cookie/租户/模块/导航鉴权，默认bootstrap兼容，DTO仍剔除scopes/account id，实际创建重新查询频道，审查通过。

前端复核发现BUG-006（深链详情被晚到列表移除）与BUG-007（自动刷新失败提示被重绘覆盖），修正后再次复核通过。独立taskCache保存详情，POST增加修订号、立即刷新列表以作废旧列表请求；旧详情GET不得覆盖新POST结果。刷新失败提示持久到成功刷新。发布前没有剩余阻断。
