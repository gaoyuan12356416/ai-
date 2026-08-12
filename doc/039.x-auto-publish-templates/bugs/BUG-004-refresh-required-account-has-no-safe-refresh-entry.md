# BUG-004 refresh_required 账号没有安全刷新入口

## 发现阶段

2026-08-12 生产 Chrome 新建模板页验收后的用户操作反馈。

## 现象

账号列表可以显示 15 个 X 账号，但它们均为 `refresh_required`，复选框全部置灰。页面没有恢复资格的入口，有模板导航权限的操作员无法在当前页面将已批准账号刷新为可发布后再选择。

## 复现步骤

1. 有模板导航权限的操作员打开 `/x-auto-publish-template.html`。
2. 账号安全快照返回 `status=refresh_required`、`publish_eligible=false`。
3. 查看账号行和复选框操作。

## 期望结果

- GET 列表保持只读，不调用 X 或刷新 Token。
- 对 `refresh_required + publish_approved=true` 的账号显示逐个“刷新账号资格”操作。
- 刷新成功并回读 `active + approved + publish_eligible` 后，该账号才可勾选。
- 未批准账号不可刷新、不可选择；临时错误可重试，明确撤销要求重新授权。

## 实际结果

页面只根据 `publish_eligible` 置灰账号，没有显式刷新入口；操作员只能离开当前流程到其他页面处理，X Auto 模板编辑器本身无法恢复可选状态。

## 根因分析

首版把账号列表设计成安全只读快照并正确禁止非当前可发布账号，但没有补充与只读 GET 分离的显式刷新动作。不能用页面加载时自动刷新替代，否则普通 GET 会调用 X、旋转 Token、放大限流并模糊审计边界。

## 修复说明

- 已增加 X Auto 精确 `/accounts/{id}/verify` 接口与“刷新可选账号资格”按钮；页面在一次操作中逐账号串行请求，不增加服务端批量接口。
- 服务端重新读取账号事实并强制 `publish_approved=true`；动态状态仍为 `refresh_required` 时才访问 X，竞态下已 active 时幂等回读，不信任浏览器提交的状态。
- 临时 X 错误保持 `refresh_required`；明确 `x_token_revoked` 转入重新授权提示。
- 刷新只恢复账号资格，不创建或修改模板，不创建 run/task/queue/log/Post，不改变三道 live gate。
- 创建、编辑、启用、执行与最终发布现有严格校验保持不变。

## 影响文件

- `features/x_auto_posts/service.py`
- X accounts / X Auto sidecar bridge
- `static/x-auto-publish-template.html`
- `static/x-auto-publish-template.js`
- `scripts/test_x_auto_post_service.py`
- `scripts/test_x_auto_publish_ui.py`
- X accounts 状态机相关回归
- `doc/039.x-auto-publish-templates/`

## 验证命令与结果

X Auto/bridge/UI 150 项通过（1 项 Windows 跳过）；既有 X 发布与账号 236/236；权限/UI 61/61；素材/剧集/媒体 129/129；catch-up/schedule 恢复 138 项通过（1 项 Windows 跳过）。Python/JavaScript 语法检查与 `git diff --check` 通过。生产 Chrome 安全验收待部署后执行。

## 回归结论

代码与离线回归完成，无评审 blocker；待部署和生产 Chrome/账本零增量验收。
