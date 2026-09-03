# SA 需求评审

结论：通过。禁止跨主机共享 SQLite，也不把完整发布 Worker/OAuth 搬到 GPU。采用 CPU 控制面 + 香港媒体数据面；复用 `127.0.0.1:18788` 隧道。上传会话 URL 只在隧道内传递且不记录；远端校验 COS host、Google upload host、task id、SHA、size 和 offset。
