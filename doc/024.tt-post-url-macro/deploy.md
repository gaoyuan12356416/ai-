# 部署文档

## 变更内容

CPU sidecar 增加 `{url}`、W2A 长链和 TT 跳转页；`gy.g2flow.com` 增加 TT 专用静态路由。GPU 业务代码不变，仅增加换行回归测试。

## 配置项

```dotenv
TT_POST_SHORT_LINK_ROOT=/mnt/data-disk/tt-post-publisher/s2l
```

## 数据库变更

启动时对 TT SQLite 执行增量列迁移和 `short_link_id>0` 唯一索引。部署前必须备份现有 SQLite 文件；新增列不删除历史数据。

## 部署步骤

1. 合并并部署已审核的 Git commit，禁止直接在服务器改源码。
2. 备份 TT SQLite。
3. 创建 `/mnt/data-disk/tt-post-publisher/s2l`，属主为 TT sidecar 用户，目录权限 `0755`。
4. 配置 `TT_POST_SHORT_LINK_ROOT`。
5. 将 `deploy/nginx-tt-short-domain-location.conf` 的 location 加入现有 `gy.g2flow.com` TLS Server，并放在 X 通用数字 `/s2l` 规则之前。
6. 执行 `nginx -t`，成功后 reload。
7. 重启 TT CPU sidecar，先保持现有发布门禁不变。

## 验证步骤

1. 检查 sidecar 健康及数据库迁移完成。
2. 创建一个仅进入队列、不触发真实发布的 `{url}` 样本。
3. 确认 `caption` 内是 gy 短链且 `\n\n` 仍存在。
4. 在临时编号上落一份合法 wrapper，执行 `curl -I` 确认 `200`、`no-store`，并确认 X 旧链接仍可访问。
5. 用户明确授权后再执行一次真实 Post，核对 TikTok 请求审计和客户端展示。

## 回滚方案

1. 关闭 TT 发布门禁并停止 sidecar。
2. 回滚应用 commit 和环境变量，重启 sidecar。
3. 移除 TT 专用 Nginx location，`nginx -t` 后 reload。
4. 新增 SQLite 列可保留；不删除已生成 wrapper，以免破坏已发送链接。
5. 如迁移导致启动失败，用部署前 SQLite 备份恢复。

## 注意事项

- 不得覆盖或移动 X 的 `/mnt/data-disk/x-post-automation/s2l` 文件。
- TT location 必须只匹配 `8` 开头的 19 位编号。
- 本文档尚未在生产执行。
