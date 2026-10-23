# AI 后台网页收入来源隔离

旧网页在同一个 Facebook 站点中仅按 campaign ID 聚合并关联后台任务。旧系统与 AI 后台复用短数字 ID，导致其他 Page/素材以及未确认成功发布的同号任务获得收入归属。

网页 JSON 和 CSV 现在明确启用 `ai_backend_only=True`，沿用独立日报已验证的 `backend_scope`：先对完整账本做唯一归属判断，再验证截至效果日的成功发布和 Facebook 冻结 Page/素材来源。YouTube 手动短链仍保留。历史内容的指定日效果与指定日成功发布数量继续分开计量。

内容范围与用户权限独立。HTTP 仍仅使用会话的 tenant_key/user_id/role，不获得 `_system_report`。全来源 scope_audit 只对内部系统采集器返回，不能经网页接口泄露其他租户或范围外金额。缺失源数据保持未知；只有原始源数据确有该日时，过滤后的零值才是已知零。CSV、总览、分组、趋势和排名使用同一份聚合。

保留含 Page/素材维度的 Facebook 查询单独缓存 5 分钟，缓存键含日期、站点、来源维度版本及全局冻结候选来源摘要，与旧 campaign 汇总缓存隔离。网页 SQL 先以完整账本的成功候选来源缩小读取范围，随后仍通过共享范围校验；不使用人员或租户筛选来消除归属冲突。SQL 同一事务返回源表实际存在的日期标记，避免没有后台流量的日期被误判未同步。独立系统采集器继续使用全来源审计。

网页保留原有 8 秒 SQL 上限、50,000 条指标上限、无自动重试、只读事务和 FIFO Gate。页面明确说明统计范围，并保留已经上线的数据报表导航命名。

## 验证

- `python -m unittest discover -s tests -p 'test_social*.py' -q`
- `python -m py_compile features/social_analytics/routes.py features/social_analytics/backend_scope.py features/social_analytics/facebook.py features/social_analytics/report.py scripts/deploy_social_web_scope.py`
- `node --check static/social-analytics.js`
- `git diff --check`

新增 HTTP 集成测试验证同号来源剔除、失败任务剔除、普通用户和租户权限、CSV 与页面金额一致、禁止请求参数关闭来源限制、数据源故障不变零及缓存隔离。原有来源范围测试覆盖未来发布、手动短链、全局归属冲突和来源维度。

使用 2026-10-22 冻结源表明细及冻结账本回放：原网页收入 508745 分，正确归属 32978 分，退款 2050 分，成功发布 244。线上验收必须读取真实登录页面和 CSV，不能把本地回放视为已经上线。

## 部署与回滚

先提交推送 GitHub，再在 CPU 拉取精确提交。`scripts/deploy_social_web_scope.py stage` 校验现网基线及独立依赖，备份范围内文件并记录发布服务 PID 和独立日报 release。后端基线为 4cda27e，网页基线为 2d97d17 的已部署导航调整；均通过 SHA256 精确验证。

生产验证待部署代码后执行：

```
python3 -B scripts/deploy_social_web_scope.py activate --backup <stage 返回的 backup>
```

仅替换主 API 的 social_analytics/routes.py、report.py、facebook.py、backend_scope.py，以及主服务和 Nginx 两处 social-analytics.html。只重启 drama-material-api.service。独立日报 release、发布任务/账本、归属采集服务及通知回执不修改。

回滚：

```
python3 -B scripts/deploy_social_web_scope.py rollback --backup <同一个 backup>
```

回滚仅恢复此次代码文件并重启主 API，不能恢复数据库。回滚会重新暴露旧网页口径缺陷，不能把旧结果当作 AI 后台专属收入。

本次不修改技能上下文或已发送历史日报。
