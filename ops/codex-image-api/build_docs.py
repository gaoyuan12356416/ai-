"""Build the standalone client documentation and OpenAPI contract, without secrets."""
import argparse
import html
import json
from pathlib import Path

BASE = "https://ai.yingliangads.com"
PATH = "/api/image-generation/tasks"
REQUEST = {"request_id": "business-request-001", "items": [
    {"image_urls": ["https://example.com/reference-1.jpg", "https://example.com/reference-2.jpg"], "prompt": "保留人物主体，制作电影风格的横版海报"},
    {"image_urls": ["https://example.com/reference-3.jpg"], "prompt": "根据参考图制作适合社交媒体的广告图片"}
]}
TASK = "ig_0123456789abcdef0123456789abcdef"
RUNNING = {"task_id": TASK, "status": "running", "done": False, "total": 3, "completed": 1, "succeeded": 1, "failed": 0, "results": None}
FINAL = {"task_id": TASK, "status": "partial_failed", "done": True, "total": 3, "completed": 3, "succeeded": 2, "failed": 1, "results": [
    {"item_index": 0, "image_index": 0, "status": "succeeded", "cos_url": "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/drama-materials/image-generation/" + TASK + "/0000-0000.png", "error": None},
    {"item_index": 0, "image_index": 1, "status": "succeeded", "cos_url": "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/drama-materials/image-generation/" + TASK + "/0000-0001.png", "error": None},
    {"item_index": 1, "image_index": 0, "status": "failed", "cos_url": None, "error": {"code": "download_failed", "message": "图片下载 HTTP 404"}}
]}


def ref(name):
    return {"$ref": "#/components/schemas/" + name}


def response(schema, description, example=None):
    content = {"schema": ref(schema)}
    if example is not None:
        content["example"] = example
    return {"description": description, "content": {"application/json": content}}


def contract():
    schemas = {
        "SubmitRequest": {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {
            "request_id": {"type": "string", "minLength": 1, "maxLength": 200, "description": "可选业务幂等键。原样复用以安全重试；相同键但不同内容返回 409。"},
            "items": {"type": "array", "minItems": 1, "items": ref("InputGroup")}
        }},
        "InputGroup": {"type": "object", "additionalProperties": False, "required": ["image_urls", "prompt"], "properties": {
            "image_urls": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}, "description": "每个 URL 独立生成一张图，组内共用 prompt。URL 的下载及格式检查在后台完成。"},
            "prompt": {"type": "string", "minLength": 1, "description": "图片要求，可包含尺寸、风格、文字及构图要求。不能仅含空白。"}
        }},
        "TaskStatus": {"type": "string", "enum": ["queued", "running", "succeeded", "partial_failed", "failed"]},
        "TaskId": {"type": "string", "pattern": "^ig_[0-9a-f]{32}$"},
        "SubmitResponse": {"type": "object", "required": ["task_id", "status", "total"], "properties": {"task_id": ref("TaskId"), "status": ref("TaskStatus"), "total": {"type": "integer", "minimum": 1}}},
        "TaskResponse": {"type": "object", "required": ["task_id", "status", "done", "total", "completed", "succeeded", "failed", "results"], "properties": {
            "task_id": ref("TaskId"), "status": ref("TaskStatus"), "done": {"type": "boolean", "description": "所有图片均已成功或失败时为 true；不等同于全部成功。"},
            "total": {"type": "integer", "minimum": 1}, "completed": {"type": "integer", "minimum": 0},
            "succeeded": {"type": "integer", "minimum": 0}, "failed": {"type": "integer", "minimum": 0},
            "results": {"type": "array", "nullable": True, "items": ref("ImageResult"), "description": "处理中为 null；终态按原提交顺序返回所有成功及失败项。"}
        }},
        "ImageResult": {"type": "object", "required": ["item_index", "image_index", "status", "cos_url", "error"], "properties": {
            "item_index": {"type": "integer", "minimum": 0, "description": "items 下标，从 0 开始"},
            "image_index": {"type": "integer", "minimum": 0, "description": "该组 image_urls 下标，从 0 开始"},
            "status": {"type": "string", "enum": ["succeeded", "failed"]},
            "cos_url": {"type": "string", "format": "uri", "nullable": True, "description": "成功图片的公开 HTTPS COS 地址；失败时为 null。"},
            "error": {"type": "object", "nullable": True, "required": ["code", "message"], "properties": {"code": {"type": "string"}, "message": {"type": "string"}}}
        }},
        "ErrorResponse": {"type": "object", "required": ["error"], "properties": {"error": {"type": "object", "required": ["code", "message"], "properties": {"code": {"type": "string"}, "message": {"type": "string"}}}}}
    }
    errors = {str(code): response("ErrorResponse", desc) for code, desc in [(400, "请求结构或 JSON 无效"), (401, "Token 缺失或无效"), (403, "来源 IP 不在白名单"), (404, "任务或接口不存在"), (409, "幂等键与已有请求内容冲突"), (413, "请求体超过 1 MiB"), (503, "服务暂不可用") ]}
    return {
        "openapi": "3.0.3", "info": {"title": "异步批量生图 API", "version": "1.0.0", "description": "仅允许来源 IP 101.32.30.18；复用现有截图 API Token。每张参考图独立生成一张图片，整批共用一个任务 ID。"},
        "servers": [{"url": BASE}], "security": [{"ScreenshotBearer": []}, {"ScreenshotApiToken": []}],
        "paths": {
            PATH: {"post": {"operationId": "submitImageGeneration", "summary": "异步提交生图批次", "requestBody": {"required": True, "content": {"application/json": {"schema": ref("SubmitRequest"), "example": REQUEST}}}, "responses": dict(errors, **{"202": response("SubmitResponse", "任务已持久化，等待后台处理", {"task_id": TASK, "status": "queued", "total": 3}), "200": response("SubmitResponse", "相同 request_id 的已有任务")})}},
            PATH + "/{task_id}": {"get": {"operationId": "getImageGenerationTask", "summary": "查询批次状态与最终 COS 链接", "parameters": [{"in": "path", "name": "task_id", "required": True, "schema": ref("TaskId")}], "responses": {"200": response("TaskResponse", "查询成功；done=true 后停止轮询", FINAL), **{k: errors[k] for k in ("401", "403", "404", "503")}}}}
        },
        "components": {"securitySchemes": {"ScreenshotBearer": {"type": "http", "scheme": "bearer", "description": "现有截图 API Token"}, "ScreenshotApiToken": {"type": "apiKey", "in": "header", "name": "X-API-Token", "description": "现有截图 API Token"}}, "schemas": schemas}
    }


PYTHON_EXAMPLE = '''import os
import time
import requests

BASE = "https://ai.yingliangads.com/api/image-generation/tasks"
headers = {"Authorization": "Bearer " + os.environ["SCREENSHOT_API_TOKEN"]}
payload = {
    "request_id": "business-request-001",  # 本次业务唯一；网络重试沿用相同值
    "items": [{
        "image_urls": ["https://example.com/reference-1.jpg"],
        "prompt": "保留人物主体，制作电影风格的横版海报"
    }]
}

# POST 网络超时不代表任务未创建；重试必须沿用相同 request_id 和内容。
for attempt in range(3):
    try:
        r = requests.post(BASE, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        task_id = r.json()["task_id"]
        break
    except (requests.Timeout, requests.ConnectionError):
        if attempt == 2:
            raise
        time.sleep(5)

while True:
    try:
        r = requests.get(f"{BASE}/{task_id}", headers=headers, timeout=30)
        r.raise_for_status()
        state = r.json()
    except (requests.Timeout, requests.ConnectionError):
        time.sleep(5)
        continue
    print(state["status"], state["completed"], "/", state["total"])
    if state["done"]:
        for item in state["results"]:
            if item["status"] == "succeeded":
                print(item["item_index"], item["image_index"], item["cos_url"])
            else:
                print(item["item_index"], item["image_index"], item["error"])
        break
    time.sleep(5)
'''


def code(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, indent=2)
    return "<pre><code>" + html.escape(value) + "</code></pre>"


def document():
    curl = '''# Linux / macOS shell；请替换 Token，并将上方 JSON 保存为 request.json。
export SCREENSHOT_API_TOKEN='YOUR_SCREENSHOT_API_TOKEN'
curl --fail-with-body -X POST 'https://ai.yingliangads.com/api/image-generation/tasks' \\
  -H "Authorization: Bearer $SCREENSHOT_API_TOKEN" \\
  -H 'Content-Type: application/json' --data-binary @request.json

curl --fail-with-body \\
  'https://ai.yingliangads.com/api/image-generation/tasks/ig_REPLACE_WITH_TASK_ID' \\
  -H "Authorization: Bearer $SCREENSHOT_API_TOKEN"'''
    sections = [
        "<section id='access'><h2>接入信息</h2><table><tr><th>服务地址</th><td>https://ai.yingliangads.com</td></tr><tr><th>来源 IP 白名单</th><td><strong>101.32.30.18</strong>，按实际连接来源校验，伪造转发头无效</td></tr><tr><th>鉴权</th><td>Authorization: Bearer &lt;现有截图 API Token&gt;<br>也支持 X-API-Token；两个方式任选其一</td></tr><tr><th>数据格式</th><td>application/json；UTF-8；请求体最大 1 MiB</td></tr><tr><th>参考图片</th><td>公开可下载的 HTTP/HTTPS 链接；单帧 PNG、JPEG、WebP；每张不超过 32 MiB；不支持视频、内网地址或需要登录的链接</td></tr></table><p>在调用方服务器上请求接口。Token 不应放入前端代码、URL 或公开文档。此处所有示例图片和任务 ID 均为占位示例。</p></section>",
        "<section id='submit'><h2>1. 提交生图批次</h2><p class='endpoint'><b>POST</b> /api/image-generation/tasks</p><p>每组填写多张参考图和一段提示词，组内<strong>每张图各生成一张结果</strong>。以下请求共生成三张图片。图片尺寸、文案、风格及构图可以写在 prompt 中。</p>" + code(REQUEST) + "<table><tr><th>字段</th><th>说明</th></tr><tr><td>request_id</td><td>可选，1–200 字符。建议提供稳定业务幂等键；同一键、同一内容返回原任务，不重复入队；内容不同返回 409。不传时每次提交均创建新任务。</td></tr><tr><td>items</td><td>必填非空数组，每组必须包含非空 image_urls 数组和非空 prompt 字符串。</td></tr></table><p>首次接收返回 <strong>HTTP 202</strong>；幂等重试命中已有任务返回 <strong>HTTP 200</strong>。响应不等待下载、生图和上传。</p>" + code({"task_id": TASK, "status": "queued", "total": 3}) + "<p>若提交响应丢失，请使用原 request_id 和原内容重试。想重新生成失败图片，应另建 request_id，仅提交需要重做的参考图。</p></section>",
        "<section id='query'><h2>2. 查询状态和结果</h2><p class='endpoint'><b>GET</b> /api/image-generation/tasks/{task_id}</p><p>建议每五秒轮询。HTTP 200 表示查询成功；图片是否生成成功请检查 status 和 results。</p>" + code(RUNNING) + "<table><tr><th>status</th><th>含义</th><th>done</th></tr><tr><td>queued</td><td>等待处理</td><td>false</td></tr><tr><td>running</td><td>正在处理，可能已有部分完成</td><td>false</td></tr><tr><td>succeeded</td><td>全部成功</td><td>true</td></tr><tr><td>partial_failed</td><td>部分成功、部分失败</td><td>true</td></tr><tr><td>failed</td><td>全部失败</td><td>true</td></tr></table><p>completed = succeeded + failed。done=true 后停止轮询；此前 results=null，整批结束后一次返回所有结果。item_index 和 image_index 均从 0 开始，结果保持原提交顺序。</p>" + code(FINAL) + "<p>成功项的 cos_url 为公开 HTTPS 图片链接；失败项保留错误原因。单张失败不会阻断其他图片。默认逐张生成，单次 Codex 生图执行上限 900 秒；排队、下载和上传耗时另计。完成结果不会因后续查询而重新生成。</p></section>",
        "<section id='errors'><h2>错误处理</h2><table><tr><th>HTTP</th><th>处理方式</th></tr><tr><td>400</td><td>修正 JSON、必填字段、请求体或 Content-Type。</td></tr><tr><td>401</td><td>检查现有截图 API Token。</td></tr><tr><td>403</td><td>确认实际出口 IP 为 101.32.30.18。</td></tr><tr><td>404</td><td>检查接口地址与 task_id。</td></tr><tr><td>409</td><td>request_id 已对应其他内容；新业务使用新 ID。</td></tr><tr><td>413</td><td>请求体超过 1 MiB，请拆分批次。</td></tr><tr><td>503</td><td>服务暂不可用；稍后重试，提交仍沿用原 request_id。</td></tr></table>" + code({"error": {"code": "unauthorized", "message": "Token 缺失或无效"}}) + "<p>任务内错误常见为 invalid_image_url / unsafe_image_url / image_too_large / download_failed / invalid_image、generation_launch_failed / generation_failed / generation_timeout / generation_no_image / execution_interrupted / invalid_output，以及 cos_not_configured / cos_object_conflict / cos_upload_failed。这些错误通过查询结果返回，不改变查询请求的 HTTP 200。</p><p>下载和 COS 上传最多尝试三次；生成图片及上传记录持久保存。已确认成功的图片不会因 worker 重启重新生成；生图中断且没有可核对产物时，记录失败，避免盲目重复执行。</p></section>",
        "<section id='examples'><h2>调用示例</h2><h3>cURL</h3>" + code(curl) + "<h3>Python：提交与轮询</h3><p>安装 requests，并设置环境变量 SCREENSHOT_API_TOKEN。替换示例参考图链接后运行。</p>" + code(PYTHON_EXAMPLE) + "</section>"
    ]
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>异步批量生图 API · 接口文档</title><style>
    :root{color-scheme:light;--ink:#16283d;--muted:#53677a;--line:#dce5ee;--accent:#106b75}*{box-sizing:border-box}body{margin:0;background:#f2f6fa;color:var(--ink);font:16px/1.75 system-ui,'Microsoft YaHei',sans-serif}main{max-width:1080px;margin:auto;padding:40px 24px 70px}header{background:#123949;color:white;border-radius:18px;padding:30px 34px;margin-bottom:24px}header p{color:#d5e6eb}h1{font-size:30px;line-height:1.35;margin:8px 0 15px}h2{font-size:23px;margin:0 0 18px}h3{font-size:18px}section{background:white;border:1px solid var(--line);border-radius:14px;padding:28px 32px;margin:20px 0;scroll-margin-top:20px}nav{display:flex;gap:10px;flex-wrap:wrap}a{color:var(--accent)}nav a{background:white;padding:7px 14px;border:1px solid var(--line);border-radius:8px;text-decoration:none}.badge{font-size:13px;letter-spacing:.06em;color:#a9dce2}table{width:100%;border-collapse:collapse;margin:18px 0}th,td{border-bottom:1px solid var(--line);padding:12px;text-align:left;vertical-align:top}th{background:#f4f8fb;font-weight:650}td:first-child{overflow-wrap:anywhere}pre{background:#0f2535;color:#e0eef7;padding:20px;border-radius:10px;overflow:auto;font:13px/1.65 Consolas,monospace;tab-size:2}p{overflow-wrap:anywhere}.endpoint{padding:12px 16px;background:#e7f5f3;border-radius:8px;font-family:Consolas,monospace}.endpoint b{color:#087569;margin-right:12px}footer{color:var(--muted);font-size:13px}@media(max-width:650px){main{padding:18px 12px}header,section{padding:22px 16px}h1{font-size:25px}th,td{padding:9px}pre{font-size:12px}}@media print{body{background:white}main{max-width:none;padding:0}nav{display:none}section{break-inside:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}}
    </style></head><body><main><header><div class="badge">API REFERENCE · 2026-11-13 · v1</div><h1>异步批量生图 API</h1><p>提交图片与提示词 → 获取一个任务 ID → 轮询状态 → 整批完成后获取 COS 链接</p></header><nav><a href="#access">接入信息</a><a href="#submit">提交接口</a><a href="#query">查询接口</a><a href="#errors">错误处理</a><a href="#examples">调用示例</a><a href="openapi.json" download>下载 OpenAPI JSON</a></nav>""" + "".join(sections) + "<footer>本文档不包含真实 Token。OpenAPI JSON 可导入兼容的接口调试工具。</footer></main></body></html>"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir")
    args = parser.parse_args()
    directory = Path(args.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "openapi.json").write_text(json.dumps(contract(), ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "异步生图API接口文档.html").write_text(document(), encoding="utf-8")
    print(str(directory))


if __name__ == "__main__":
    main()
