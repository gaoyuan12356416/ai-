# 部署文档

## 变更内容

- 新增独立公开静态路径 `/reports/opay-excellent-creatives/`。
- 新增 `/opt/opay-excellent-creatives/releases/<commit>` 与 `current`。
- 新增数据盘缓存/快照/缩略图和公开静态目录。
- 新增初版、终版两个月度 timer；不修改旧报表 unit。

## 配置项

生产环境文件 `/etc/opay-excellent-creatives.env`，`0600 root:root`。配置仅保存路径、超时和媒体允许主机等非公开运行参数；MySQL 凭据继续由现有本机只读命令模块提供，禁止进入 GitHub/日志。

## 数据库变更

无 MySQL DDL/DML。所有查询必须在 `101.32.56.53:63350` 验证 `@@read_only=1` 后执行；写端口 63353 不在本需求范围。

## 部署步骤

1. 本地完成编译、单元、前端契约和 `git diff --check`。
2. 提交并推送分支，记录精确 GitHub commit。
3. 在服务器验证 `/mnt/data-disk` 为 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8` 的已挂载可写文件系统并检查空间。
4. 验证 GitHub SSH，fetch 精确 commit 到新的不可变 release；不得从本地直接复制源代码伪装成 GitHub 发布。
5. 在 `/mnt/data-disk/opay-excellent-creatives/backups/<timestamp>-pre-<sha>` 备份旧 current、公开提交点、Nginx、env 和 units，生成并校验 SHA-256 清单。
6. 安装新 env、Nginx 和 systemd 文件，执行服务器端编译/测试。
7. `nginx -t` 成功后 reload Nginx；先不启用 timer。
8. 使用影子输出完成 2026-07 回归和媒体抽样，再回填 `2026-01` 至最近完整月份。
9. 检查每月快照后原子发布，最后切换 `current` 和 `latest.json`。
10. 启用并启动两个 timer，完成公开、旧系统和只读抽样验收。

## 验证步骤

```bash
python3 -m py_compile /opt/opay-excellent-creatives/current/ops/opay-excellent-creatives/opay_excellent_creatives.py
python3 -m unittest discover -s /opt/opay-excellent-creatives/current/ops/opay-excellent-creatives -p 'test_*.py' -v
python3 /opt/opay-excellent-creatives/current/ops/opay-excellent-creatives/validate_frontend_contract.py
sqlite3 /mnt/data-disk/opay-excellent-creatives/cache/opay-excellent-creatives.sqlite3 'PRAGMA quick_check;'
nginx -t
systemctl status opay-excellent-creatives-initial.timer --no-pager
systemctl status opay-excellent-creatives-final.timer --no-pager
curl -sS -I https://ai.yingliangads.com/reports/opay-excellent-creatives/
curl -sS https://ai.yingliangads.com/reports/opay-excellent-creatives/latest.json
```

还需验证匿名 200/无 Location、robots、2026-01 起月份清单、Google 无估算、Meta/TikTok 非空、素材 ID/产品范围、公开 JSON 不含密码/Token、主 API和旧 AI Game Performance 行为不变。

## 回滚方案

1. 停止并禁用两个新 timer，确认 oneshot service 不在运行。
2. 将 `current` 原子切回备份记录的上一 release；若首次部署则移走新 current，不删除。
3. 恢复备份的 Nginx/env/units/公开 `index.html` 和 `latest.json`，清单最后恢复。
4. `systemctl daemon-reload && nginx -t && systemctl reload nginx`。
5. 保留 SQLite、快照和缩略图用于审计，不在常规代码回滚中恢复或删除数据盘事实。
6. 复核新路径状态（首次部署回滚应为 404）、旧报表、主 API、Nginx 和磁盘。

生产发布完成后在本文件补充精确 commit、release、备份路径、前一版本和可直接执行的回滚命令。

## 注意事项

- 公开无鉴权是已确认产品决策；`noindex` 不等于保密。
- `latest.json` 必须最后切换。
- 最终月份冻结后禁止普通 timer 改写；修复历史数据必须显式 `--rebuild` 并保留差异审计。
- 缩略图/源文件异常不得阻塞整月；数据库、AF 配置或发布提交失败必须阻塞并保留旧版本。
