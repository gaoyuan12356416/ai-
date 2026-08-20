# 开发计划

## 开发范围

仅调整 FB 自动发布模板静态前端、对应契约测试和文档。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 冻结列表优先交互 | PM/SA | requirements.md / sa-review.md | 完成 |
| 拆分列表与表单页 | Codex | `static/fb-auto-publish-*` | 完成 |
| 更新前端契约测试 | Codex | `scripts/test_fb_auto_app_contract.py` | 完成 |
| 本地回归与浏览器 QA | QA | 测试命令 / Playwright | 完成 |
| GitHub 与生产静态发布 | Codex | 分支 / CPU 静态目录 | 待执行 |

## 编译 / 构建命令

```powershell
node --check static\fb-auto-publish-common.js
node --check static\fb-auto-publish-templates.js
node --check static\fb-auto-publish-template.js
python -m unittest scripts.test_fb_auto_app_contract
python -m unittest discover -s scripts -p "test_fb_auto_*.py"
git diff --check
```

## 风险与依赖

- 本地浏览器回归需要模拟已登录且具备 `fb_page_posts` 权限的 API 响应，严禁通过创建生产模板验收。
- 生产为非 Git 工作目录，必须先备份原静态文件，再从 GitHub 精确提交导出新增文件。

## 完成记录

- 2026-08-20：三个新增脚本 `node --check` 通过；FB 全量 80/80、X/TT 主契约 66/66 通过。
- 2026-08-20：Playwright mock 登录态完成列表、创建、编辑、保存、筛选、分页、确认、权限和 390×844 回归，控制台 0 error/0 warning。
