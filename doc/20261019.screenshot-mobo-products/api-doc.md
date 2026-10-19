# API 与 CLI

GET /api/drama-screenshot-material/products：cover_synthesis 权限，200 {"items":[{"app_id":"...","app":"...","country":"...","language":"...","label":"..."}]}。顺序 1479、979、3360、2373。原 /api/drama-material/products 不变。

POST /api/drama-screenshot-material/jobs 和 /jobs/batch 保持现有字段、鉴权、202 回执与重试接口。手动任务不受自动消耗条件限制。

python3 scripts/auto_submit_cover_synthesis.py --all-products [--dry-run] [--date YYYY-MM-DD]

--profile dramawave|moboreels|moboshort 可独立运行固定规则。profile 不受旧 min_spend/min_roas 环境默认值影响。未选择 profile 的旧 CLI 保持兼容。退出码非零表示至少一个产品失败；JSON products 逐产品给出筛选条件和提交/跳过/失败记录。
