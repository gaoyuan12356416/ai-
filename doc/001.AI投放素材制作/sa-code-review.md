# SA 代码评审

## 结论

通过。当前实现已覆盖投放素材任务管理的主流程、权限边界、状态流转、前端入口和最终上报适配。真实 CPU/GPU 生成命令和最终上报 token 仍属于部署期配置，不进入仓库。

## 评审范围

- `app.py`
- `static/index.html`
- `static/quick-nav.js`
- `.env.example`
- `deploy/drama-material-api.service`
- `doc/001.AI投放素材制作/`

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 处理结果 |
| --- | --- | --- | --- | --- |
| SA-CODE-001 | 高 | `app.py` 投放素材任务读取 | 初版在读取任务时持有 `JOB_DB_LOCK` 又读取素材子表，普通 `threading.Lock` 会形成自锁等待 | 已拆分任务行读取和素材子表读取，避免嵌套加锁 |
| SA-CODE-002 | 中 | `app.py` MySQL 表结构探测 | 本地 Windows 无 mysql 客户端时会输出异常堆栈，影响本地验证判断 | 已将 `FileNotFoundError` 降级为 warning |
| SA-CODE-003 | 高 | 配置和文档 | 用户提供的 Bearer token 不能提交到仓库 | `.env.example` 留空，代码只读取 `AD_MATERIAL_SOURCE_API_TOKEN` |

## 验证结果

```bash
python -m py_compile app.py
node --check static/quick-nav.js
```

前端内联脚本已通过 Node 语法解析。后端使用临时 SQLite 完成任务创建、需求生成、需求审核、素材生成、素材审核、权限隔离、编辑锁定、复制和删除规则烟测。

## 剩余联调项

- 在线 CPU 环境验证 `admin_user_group.email -> sub_user_id` 映射。
- 在线 GPU 环境配置并验证真实素材生成命令。
- 带真实 token 调用最终素材上报接口，确认返回体字段。
