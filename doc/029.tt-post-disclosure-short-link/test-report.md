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

待部署后补充 release、服务、Nginx、历史 TT/X 链接 hash 和只读数据库核对结果。不执行真实 TikTok 发布。
