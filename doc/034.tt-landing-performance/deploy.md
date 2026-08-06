# 部署文档

## 变更内容

- 发布 23 份 locale HTML、内容哈希 JS、单语言 JSON 与内容哈希 WebP。
- Nginx 对 `/tt` 和 `/tt-code` 按白名单语言选择首屏，启用 gzip 与静态缓存。
- 新增独立资产生成服务，从原 Featured 定时任务的 v2 LKG 派生 v3
  单语言产物和缩略图；原 v1/v2 服务与 timer 不修改。

## 配置项

- locale：`/mnt/data-disk/tt-drama-featured/public/by-language`
- WebP：`/mnt/data-disk/tt-drama-featured/public/covers`
- 独立代码指针：`/mnt/data-disk/tt-code-performance/current`
- 固定 Python：`/mnt/data-disk/tt-code-performance/venv-pillow-11.3.0/bin/python`
- 固定公开路径：`/tt-featured-covers/<sha256>.webp`
- 图片超时、最大字节和并发数均由代码限制；并发最大为 4。

## 数据库变更

无。数据库、Redis、code 表和发布队列均不变。

## 生产部署记录（2026-08-06）

- 分支：`codex/tt-landing-performance-20260806`
- 功能提交：`3a3a861bd24b805983748bae13a48000f9c884e0`
- 生产运行提交：`6af3939c88d9696fd8b09b6dbc742928eb1d31df`
- release：`/mnt/data-disk/tt-code-performance/releases/6af3939c88d9696fd8b09b6dbc742928eb1d31df`
- current：`/mnt/data-disk/tt-code-performance/current` 精确指向上述 release。
- 部署前备份：
  `/mnt/data-disk/tt-code-performance/backups/20260806T121304+0800-3a3a861bd24b`
- 备份目录权限为 `0750`，`pre-deploy.sha256` 全部校验通过。
- 备份名保留首次功能提交短 SHA；生产运行提交随后只补充了兼容的官方
  Pillow wheel hash，备份仍是切流前的精确快照。
- 数据盘 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`，ext4 读写挂载；
  部署后剩余约 149 GiB。
- Pillow `11.3.0` 的 WebP 实际编码 smoke test 通过。
- 首次生成 22 份语言 JSON、109 次封面转换全部成功，得到 104 个去重
  WebP；第二次幂等运行 `locale_changed_count=0`、
  `thumbnail_changed_count=0`。
- 独立 `.path` 已启用并处于监听状态；原 Featured timer 保持启用/运行。
- `nginx -t` 通过后仅 reload；主 Nginx、API、Redis 和 TT Post 均未因本次
  部署重启。

## 生产文件指纹

- 兼容 HTML：`90fc7dcca0e79d46c41b7ec98eade51a5ae42303462fe700b80e9935d3f8c197`
- hash JS 与兼容 JS：
  `e907e1e2a988145ae4658749582014a7992371a26ac48ff3eb883ba16a88e3dc`
- locale map：`40f4a3925de2278ab17e6799278351f0dc1a78213d6c12847819e8ecfa85d428`
- `/tt` Nginx 配置：
  `deb5cd352acf2f3e42504496f05108f6a3c703a3598c3c59435ec5e927c33f47`
- resolver Nginx 配置：
  `38311127550ed62189755dc2bebe73935e88d181796ab40ff5251d85a4d6ae38`

## 验证结果

- `/tt`、`/tt-code`、23 语言首屏、旧全语言接口、单语言接口与 WebP 均通过。
- HTML/JS/JSON 均按配置 gzip；HTML 保持 `no-store`，hash JS/WebP 为
  `public, max-age=31536000, immutable`。
- `/tt/`、`/tt-code/` 和路径穿越请求继续返回 404。
- 真实 Chrome 冷启动、5 条动态榜单、Search 与 Featured 点击拦截均通过。
- 现网四字符 code 解析为 `code_exact`，八个归因参数完整，
  `af_channel=TT`；活动生产 release 的 17 项 code/macro 测试通过。
- `nginx`、`drama-material-api`、`tt-code-redis`、`tt-post-service`、原
  Featured timer 和新资产 `.path` 均为 active，关键服务 `NRestarts=0`。

## 精确回滚方案

1. 禁用新监听器：
   `systemctl disable --now tt-drama-featured-assets.path`。
2. 从部署前备份恢复：
   `tt-drama-search.conf.before`、`tt-drama-code-search.conf.before`、
   `tt-drama-code-search.html.before`、`tt-drama-code-search.js.before`。
3. 将新增的
   `/etc/nginx/conf.d/tt-drama-code-locale-map.conf` 移入上述备份目录保留审计；
   切流前该文件不存在。
4. 执行 `nginx -t`，通过后只 reload Nginx，并轮询确认新 worker 生效。
5. `/mnt/data-disk/tt-code-performance/current` 可改名保留审计；旧配置不引用
   它。v3 JSON/WebP、release 和 venv 均保留，不做递归删除。
6. 不回滚或删除 SQLite、Redis、code、发布队列及原 Featured 资源。

## 注意事项

- 必须先发布数据与 hash 资产，再切换 HTML；反向顺序会产生短暂 404。
- Nginx reload 后要轮询，单次旧 worker 响应不作为失败依据。
- 最终跳转测试统一在浏览器内拦截，没有真实访问 W2A，也没有触发发布。
