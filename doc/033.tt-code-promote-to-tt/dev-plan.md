# 开发计划

## 任务拆分

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| 生产与本地只读基线审计 | Nginx、静态文件、服务、缓存 | 已完成 |
| `/tt` alias 与安全头升级 | 两份 Nginx 配置 | 已完成 |
| Node/Python 路由合同更新 | bridge 与 app contract tests | 已完成 |
| `/tt` 真实浏览器主流程与 `/tt-code` 兼容 smoke | browser regression | 已完成 |
| `{code}` 正式自动排期到 GPU 请求闭环 | TT Post service tests | 已完成 |
| 代码评审与完整回归 | 相关测试集 | 已完成 |
| GitHub-first 发布、备份、线上验收 | 43.166.187.96 | 待执行 |

## 实施顺序

1. 只读确认生产 `/tt`、`/tt-code`、静态文件哈希、Nginx 配置和并行 release。
2. 修改路由和合同测试，保留旧文件不动。
3. 补 `{code}` 自动排期、直接测试边界和 GPU payload 回归。
4. 执行 Node、Python、浏览器、编译与 diff 检查。
5. 独立代码评审，无开放 P0/P1 后提交并推送 GitHub。
6. 服务器从精确提交建立只读 release，备份旧配置后只部署一个 Nginx 文件。
7. `nginx -t`、reload、HTTP/浏览器/接口验收；异常立即恢复备份。

## 本地验证命令

```powershell
node scripts/test_tt_drama_code_bridge.js
node scripts/test_tt_drama_bridge.js
python -m unittest tests.test_tt_drama_resolver_app_contract -v
python -m unittest scripts.test_tt_posts_service -v
python -m unittest scripts.test_tt_posts_core scripts.test_tt_post_code_routes -v
node scripts/test_tt_drama_code_browser.js
python -m compileall -q features scripts tests
git diff --check
```

## 发布边界

- 仅部署 `deploy/nginx/tt-drama-search.conf`。
- 不全量复制仓库，不覆盖任何旧静态文件。
- 不切换 `/mnt/data-disk/tt-drama-resource-cache/current`。
- 不切换 `/opt/tt-post/current`，不重启或触发 TT 发布。
