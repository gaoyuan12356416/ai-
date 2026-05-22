# 部署说明

## 新增文件

- `app.py`：新增配音剧语种任务接口、外部 KOL 任务接口转发、设计师下拉、素材筛选口径。
- `static/index.html`：新增页面、筛选结果列表和批量创建弹窗。
- `static/quick-nav.js`：新增快速导航入口。
- `.env.example`：新增外部接口配置占位。

## 环境变量

```bash
VOICEOVER_KOL_TASK_API_URL=https://ads-admin.static.kunlun.com/api/ai/kol-task
VOICEOVER_KOL_TASK_API_TOKEN=
VOICEOVER_KOL_TASK_API_TIMEOUT=30
VOICEOVER_DESIGNER_ROLE_APP_ID=78
VOICEOVER_DEFAULT_ROAS_THRESHOLD=45
VOICEOVER_DEFAULT_MIN_CANDIDATES=15
VOICEOVER_DEFAULT_APP_ID=
```

真实 token 只配置在服务器 `/root/drama_material_service/.env`，不得提交仓库。

## 部署步骤

1. 将变更同步到 `/root/drama_material_service`。
2. 同步静态文件到 nginx web root `/usr/share/nginx/html`。
3. 配置 `VOICEOVER_KOL_TASK_API_TOKEN`。
4. 重启 `drama-material-api.service`。
5. 验证：
   - `python -m py_compile app.py`
   - `node --check static/quick-nav.js`
   - 内联脚本语法解析
   - `GET /api/auth/status`
   - 登录后打开 `/#voiceoverTasks`
