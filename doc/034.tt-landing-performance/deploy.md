# 部署文档

## 变更内容

- 发布 locale HTML、内容哈希 JS、单语言 JSON 与内容哈希 WebP。
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

## 部署步骤

1. 记录当前 GitHub commit、生产文件 SHA、Nginx 配置和服务状态。
2. 在数据盘创建带 manifest 的时间戳备份，确认 UUID 与空间。
3. 从精确 GitHub commit 创建不可变 release，并原子切换本功能独立的
   `/mnt/data-disk/tt-code-performance/current`；不切原资源缓存 current。
4. 使用带 SHA-256 锁定的 Pillow 11.3.0 wheel 建立数据盘 venv，并先
   运行独立 Featured 资产服务，校验 v3 JSON 和 WebP。
5. 发布 hash JS 与 locale HTML，再安装最小 Nginx 配置。
6. `nginx -t` 后仅 reload Nginx；按轮询确认新 worker 生效。
7. 启用独立 `.path` 监听 v2 LKG 的原子更新；不重启主 API、TT Post、
   Redis 或原 Featured oneshot/timer。

## 验证步骤

- 核对 HTML/JS/JSON/WebP HTTP 状态、SHA、Content-Language、Cache-Control、Content-Encoding。
- 验证 `/tt`、`/tt-code`、尾斜杠、全语言旧接口和单语言新接口。
- 运行 390x844 Chrome 冷/热启动及搜索/Featured 点击拦截测试。
- 检查 `tt-drama-featured-assets.service/.path`、原 Featured service/timer、
  Nginx 和原 TT/资源服务健康。

## 回滚方案

- 恢复备份的两个 TT Nginx 配置，`nginx -t` 后 reload。
- 恢复此前静态 HTML/JS 或让旧配置重新指向原文件。
- 禁用本次新增资产 `.path`，恢复或移出新增 unit 后 daemon-reload；保留
  v3 JSON/WebP 供审计，不删除数据。
- 不回滚或删除 SQLite/Redis/发布记录。

## 注意事项

- 必须先发布数据与 hash 资产，再切换 HTML；反向顺序会产生短暂 404。
- Nginx reload 后要轮询，单次旧 worker 响应不作为失败依据。
- 部署完成后补充精确 commit、release、备份路径、manifest 和验证结果。
