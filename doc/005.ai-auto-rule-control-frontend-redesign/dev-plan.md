# 开发计划

## 开发范围
本阶段已从可评审静态原型推进到正式前端实现：重写 `ad-control-rules.html` 对应交互为“规则组管理 + 抽屉式创建/编辑”，并复用现有 API 写入规则集、账户池和绑定关系。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 梳理现有前端问题 | Codex | `static/ad-control-pages.js` | 完成 |
| 创建需求目录 | Codex | `doc/005...` | 完成 |
| 设计规则组管理原型 | Codex | `prototype/ad-control-redesign-preview.html` | 完成 |
| 补充产品枚举和账号加载口径 | Codex | `requirements.md`、原型 | 完成 |
| 补充评审文档 | Codex | `requirements.md`、`sa-review.md` | 完成 |
| 用户评审 | 用户 | 实现页面截图和线上页面 | 待确认 |
| 正式实现 | Codex | `static/ad-control-pages.js`、`static/ad-control-pages.css`、导航配置 | 完成 |

## 编译 / 构建命令

```bash
python -m py_compile app.py
node --check static/quick-nav.js
node --check static/ad-control-pages.js
```

## 风险与依赖
- 用户仍需确认最终交互是否符合运营使用习惯。
- 当前实现未启用自动 runner，不会自动触发广告动作。
- 浏览器验收使用 mock API，不代表线上业务库账号数量和接口耗时。
- 后续实现必须继续遵守 GitHub-first 和线上备份流程。

## 完成记录
2026-07-01：完成静态原型，等待评审。
2026-07-01：按反馈调整为规则组管理口径，补充产品枚举限制和账号按产品加载。
2026-07-01：纠正产品口径，产品下拉只保留 `dramawave`、`hotdrama`、`freereels` 三个值，不展示或派生其他产品。
2026-07-01：正式实现规则组列表 + 抽屉式创建/编辑；产品固定三枚举，账号按产品加载，保存时映射到现有规则集、账户池和绑定关系。
