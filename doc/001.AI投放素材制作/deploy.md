# 部署文档

## 变更内容

- AI 后台新增投放素材任务管理页面。
- 新增投放素材任务和素材 SQLite 表。
- 新增最终素材上报接口配置。
- 新增可插拔 CPU 需求生成命令和 GPU 素材生成命令配置。
- 新增权限模块 `ad_material_tasks`。

## 配置项

`.env` 需要新增：

```bash
AD_MATERIAL_SOURCE_API_URL=https://aa.yingliangads.com/api/material/source
AD_MATERIAL_SOURCE_API_TOKEN=
AD_MATERIAL_SOURCE_API_TIMEOUT=30
AD_MATERIAL_REQUIREMENT_COMMAND=
AD_MATERIAL_GENERATION_COMMAND=
AD_MATERIAL_WORK_ROOT=/root/ad_material_tasks
AD_MATERIAL_PUBLIC_ROOT=/usr/share/nginx/html/ad-materials
AD_MATERIAL_PUBLIC_BASE_URL=https://ai.yingliangads.com/ad-materials
```

真实 token 只写入服务器 `.env`，不提交 GitHub。

## 数据库变更

服务启动时自动创建/迁移 SQLite 表：

- `ad_material_task`
- `ad_material_asset`

业务 MySQL 只读查询：

- `ads_apps_setting`
- `admin_role_apps`
- `admin_role_users`
- `admin_user_group`

## 部署步骤

1. 拉取新分支或合并 PR。
2. 在服务器 `/root/drama_material_service/.env` 补充配置项。
3. 确认最终素材上报 token 已配置。
4. 如接入真实生成链路，配置 `AD_MATERIAL_REQUIREMENT_COMMAND` 和 `AD_MATERIAL_GENERATION_COMMAND`。
5. 重启服务：

```bash
systemctl restart drama-material-api.service
```

6. 查看日志：

```bash
journalctl -u drama-material-api.service -n 200 --no-pager
```

## 验证步骤

1. admin 登录后台。
2. 打开快速导航“投放素材任务管理”。
3. 授权普通用户页面权限。
4. 普通用户创建 1 个任务。
5. 发布任务并观察状态进入需求待审核。
6. 驳回需求，确认原因必填且重新生成。
7. 审核通过需求，进入素材生成。
8. 审核素材，全部通过后执行上报。
9. 确认每条素材上报结果写回页面。

## 回滚方案

- 回滚代码到上一版本。
- 移除快速导航新增入口或撤销用户权限。
- 不删除 SQLite 表，保留任务数据以便排查。

## 注意事项

- 不要在部署文档、提交说明、PR 描述中写真实 token。
- 如果真实 AI/GPU 命令未配置，系统只会跑占位链路，用于页面和状态机验证。
