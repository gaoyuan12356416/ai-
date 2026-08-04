# 测试报告

## 本地结果

| 项目 | 结果 |
| --- | --- |
| Python 编译 | 通过 |
| 9 个 TT Python 测试脚本 | 347/347 通过 |
| Node drama bridge | 53/53 断言通过 |
| `git diff --check` | 通过 |

执行命令：

```text
python -m py_compile app.py features/tt_posts/core.py features/tt_posts/links.py features/tt_posts/service.py scripts/tt_post_prepare_runner.py
python -m unittest scripts.test_tt_account_settings_ui scripts.test_tt_gpu_worker scripts.test_tt_posts_app_contract scripts.test_tt_posts_core scripts.test_tt_posts_service scripts.test_tt_post_direct_config_core scripts.test_tt_post_links scripts.test_tt_post_pool_ui scripts.test_tt_post_prepare_runner
node scripts/test_tt_drama_bridge.js
```

## 增量覆盖

- 新自动任务的短链 ID 等于 `tt_post_queue.id`，URL 为 `/s2l/tt/{id}.html`。
- 两个并发写入者各自获得唯一 queue ID，且各自链接 ID 与 queue ID 一致。
- 历史 19 位 TT URL 仍可构建、校验和访问原路径。
- 新短链文件原子写入、幂等重放和并发冲突保护。
- 自动任务无论账号设置和旧请求是否传入披露，队列与 GPU 发布请求均为双 `false`。
- 模拟部署前旧队列双 `true`，`begin_publish` 后双 `false`。
- Nginx 同时包含新 `/s2l/tt/` 和历史 19 位 TT 路由。

## 生产验证

- release `e11305771246dea484f3a11c5a62dfc46a60b9fb` 已上线。
- 服务健康、Nginx 配置和新旧路由合同通过。
- 历史 X `/s2l/6.html` 与旧 TT 19 位链接的公开内容 hash 与部署前一致。
- 部署前后 queue CSV 无差异，SQLite 完整性通过，活动 queue 数为 0。
- 14:40 自然 runner tick 返回 `status=ok` 且发布请求数为 0。
- 未创建生产 queue、未创建新短链文件、未执行真实 TikTok 发布。
