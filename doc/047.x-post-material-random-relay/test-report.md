# 测试报告

## 测试结论

专项、focused 与全量 X 回归通过；Business/SA Gate 与 QA Gate 均为 PASS，GitHub-first 生产发布门禁通过。

## 测试范围

runner/OAuth/store、Premium relay 状态机、multi-schedule、账号语言、账号 Sidecar 合同。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 新增专项 | 15 | 15 | 0 | 0 |
| focused regression | 210 | 210 | 0 | 0 |
| 全部 `test_x_*.py` | 685 | 683（另 2 条条件跳过） | 0 | 0 |

## 缺陷情况

无确认未关闭缺陷；未创建 bug 文件。

## 验证证据

```text
python -m unittest scripts.test_x_post_material_random_relay -v
Ran 15 tests in 0.601s ... OK

python -m unittest scripts.test_x_post_material_random_relay scripts.test_x_post_schedule_runner scripts.test_x_post_premium_relay_repost scripts.test_x_post_multi_schedule_store scripts.test_x_account_language_routing scripts.test_x_accounts
Ran 210 tests in 24.191s ... OK

python -m unittest discover -s scripts -p 'test_x_*.py'
Ran 685 tests in 35.367s
OK (skipped=2)

python -m py_compile features/x_posts/service.py features/x_accounts/oauth_service.py scripts/x_post_schedule_runner.py scripts/test_x_post_material_random_relay.py
exit 0

git diff --check
exit 0（仅显示 Git 的 LF/CRLF 转换提示，无 whitespace error）
```

## 遗留风险

- 未在生产 Token/媒体/网络上执行真实写入验证，符合授权边界。
- relay entitlement 是动态事实，运行时仍会在建计划与最终写入前按当前 Token 复核。
- Linux 临时目录无法通过生产固定 data-disk work-directory guard；服务器改用 182 条核心 focused 回归并独立验证真实 workdir，未为测试放宽安全边界。
- 切换首分钟 X Auto 与 sidecar 同秒启动出现一次瞬时 unavailable；后续自然周期自行恢复且连续成功。
- 16:59 的既有自然 material run `217` 在交付收口时仍处于媒体预检：第二个修复输出约 456 MB，GPU 转码已完成并在上传 COS；配置的单次 repair timeout 为 3600 秒。此后台任务 queue=0、log/unknown 无新增，不中断、不人工重试，也不作为代码/部署验收的阻塞门禁。

## 发布建议

已按 deploy.md 部署 exact commit；禁止额外真实 Post/Repost canary，以自然 timer 与 ledger 不变量作为生产验收。
