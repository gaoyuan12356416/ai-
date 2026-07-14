# SA 测试用例评审

## 结论

通过。用例覆盖鉴权、OAuth安全、多账号、Token生命周期、敏感数据和生产导航。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| STR-001 | Token生命周期 | 只测 Access Token 未覆盖 Refresh Token轮换 | TC-011验证保存新 Refresh Token | 已补充 |
| STR-002 | 公网暴露 | 未验证 internal 路由无法从公网访问 | 增加 Nginx公网404检查 | 已补充 |
| STR-003 | 用户文案 | “登录时间”含义可能误导 | 页面拆分首次/最近授权等字段 | 已补充 |
| STR-004 | 并发 | 未覆盖双刷新和 callback-vs-verify | 新增两类并发测试，确保只刷新一次且重新授权最终生效 | 已补充 |
| STR-005 | Token属主 | 未覆盖空/错误 `/users/me` ID | 新增 identity mismatch测试 | 已补充 |
| STR-006 | 配置 | 未覆盖环境删减必需 scope | 新增 fail-closed测试 | 已补充 |
| STR-007 | Header泄漏 | 未覆盖 30x Authorization转发 | 新增双服务器 redirect测试 | 已补充 |

## QA 修订确认

已纳入 TC-011、TC-014、TC-016及 16 项自动化测试与生产验证步骤。
