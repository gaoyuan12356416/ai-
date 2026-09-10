# 2026-09-10 素材筛选配置启用

用户补充created_at下限，并明确授权先启用此SQL。当前筛选条件：data_source='6'、task_id='-2'、product='dramawave'、designer='789'、created_at>='2026-09-10'。未添加type/is_delete/额外日期或语言过滤。

配置来源：`deploy/youtube-auto-material-source.sql`；生产目标：`/etc/youtube-auto-publish/material-source.sql`。先GitHub提交，再服务器取exact commit中的文件，备份后原子替换，0600权限。读取配置热生效，不重启服务。

投影映射：id/name/url/cover/data_source_id/language/video_duration均由实库schema和现有代码核对。只将可信HK COS原地址的http改成https，保持同一域名、对象路径和参数；视频时长秒数转分:秒。name投影明确COLLATE utf8mb4_general_ci，与搜索表达式统一，避免实库utf8mb4_bin产生1267排序规则冲突。缺失原合成关联/简介/文件大小不伪造，task_id=-2不能充当原合成job_id。

只读验证：原始新SQL命中3条，接口映射后同样3条（6617751、6617770、6617776），每条时长26:38，查询约2.0秒。3个HTTPS视频HEAD均200 video/mp4，没有下载视频或执行发布。未执行数据库DDL、没有延长8秒SQL上限。

此前缺日期下限的SQL触发MySQL3024超时；最新用户提供的日期条件解决了当前查询性能问题，该旧诊断不再构成本次启用阻塞。

## 生产执行结果

配置提交 `136c42e744daef4c9cea778f9a91c0417c9dc677` 已推送并在CPU由GitHub取回。生产SQL摘要 `c301d02c6bfad8fa62b86cc2befe83176a38616c4d42c9e385bfb78e0e838e01`。配置读回configured=true，素材3条，按ID6617751搜索返回1条，按Dragon搜索返回3条，频道25个。没有重启服务，没有新增发布/通知测试。

原配置备份：`/mnt/data-disk/deploy/youtube-auto-publish/backups/material-sql-20260910-164158/material-source.sql`。

仅回滚本次筛选配置（不涉及应用/数据库）：

```bash
cp -p /mnt/data-disk/deploy/youtube-auto-publish/backups/material-sql-20260910-164158/material-source.sql /etc/youtube-auto-publish/material-source.sql
```

页面重新加载后读取新配置，20秒素材缓存以配置文本为key自动失效。
