#!/usr/bin/env python3































import base64
import json

import concurrent.futures































import hashlib































import hmac































import logging































import os































import re































import secrets































import shutil
import socket































import sqlite3































import subprocess































import tempfile































import threading































import time































import traceback































import uuid

import unicodedata































import wave































from datetime import datetime, timedelta































from http import cookies































from http.server import BaseHTTPRequestHandler, HTTPServer































from socketserver import ThreadingMixIn































from urllib.parse import parse_qs, quote, urlencode, urlparse































































import requests

try:

    from qcloud_cos import CosConfig, CosS3Client

except Exception:

    CosConfig = None

    CosS3Client = None





























































MYSQL_HOST = (
    os.environ.get("DRAMA_DB_HOST")
    or os.environ.get("ADMIN_MAPPING_MYSQL_HOST")
    or ""
).strip()
MYSQL_PORT = (
    os.environ.get("DRAMA_DB_PORT")
    or os.environ.get("ADMIN_MAPPING_MYSQL_PORT")
    or ""
).strip()
MYSQL_USER = (
    os.environ.get("DRAMA_DB_USER")
    or os.environ.get("ADMIN_MAPPING_MYSQL_USER")
    or ""
).strip()
MYSQL_PASSWORD = (
    os.environ.get("DRAMA_DB_PASSWORD")
    or os.environ.get("ADMIN_MAPPING_MYSQL_PASSWORD")
    or ""
)

MYSQL_BASE_CMD = ["mysql"]
if MYSQL_HOST:
    MYSQL_BASE_CMD.extend(["-h", MYSQL_HOST])
if MYSQL_PORT:
    MYSQL_BASE_CMD.extend(["-P", MYSQL_PORT])
if MYSQL_USER:
    MYSQL_BASE_CMD.extend(["-u", MYSQL_USER])
MYSQL_BASE_CMD.extend(["-N", "-B", "--default-character-set=utf8mb4", "-e"])
DB_NAME = os.environ.get("DRAMA_DB_NAME", "kunlunads_dev")































SOURCE_TABLE = os.environ.get("DRAMA_SOURCE_TABLE", "ads_drama_resource")































JOB_DB_PATH = os.environ.get(































    "DRAMA_JOB_DB_PATH", "/root/drama_material_service/data/drama_material_jobs.sqlite3"































)































HOST = os.environ.get("DRAMA_API_HOST", "0.0.0.0")































PORT = int(os.environ.get("DRAMA_API_PORT", "8787"))































WORK_ROOT = os.environ.get("DRAMA_WORK_ROOT", "/root/drama_material_jobs")































PUBLIC_ROOT = os.environ.get("DRAMA_PUBLIC_ROOT", "/usr/share/nginx/html/drama-materials")































PUBLIC_BASE_URL = os.environ.get(





























    "DRAMA_PUBLIC_BASE_URL", "https://ai.yingliangads.com/drama-materials"





























)

NAVIGATION_CONFIG_PATH = os.environ.get(

    "DRAMA_NAVIGATION_CONFIG_PATH", "/usr/share/nginx/html/navigation.json"

)

SCREENSHOT_WORK_ROOT = os.environ.get(

    "DRAMA_SCREENSHOT_WORK_ROOT", "/root/drama_screenshot_jobs"

)

SCREENSHOT_PUBLIC_ROOT = os.environ.get(

    "DRAMA_SCREENSHOT_PUBLIC_ROOT", "/usr/share/nginx/html/drama-screenshot-materials"

)

SCREENSHOT_PUBLIC_BASE_URL = os.environ.get(

    "DRAMA_SCREENSHOT_PUBLIC_BASE_URL", "https://ai.yingliangads.com/drama-screenshot-materials"

)


AD_MATERIAL_WORK_ROOT = os.environ.get(
    "AD_MATERIAL_WORK_ROOT", "/root/ad_material_tasks"
)
AD_MATERIAL_PUBLIC_ROOT = os.environ.get(
    "AD_MATERIAL_PUBLIC_ROOT", "/usr/share/nginx/html/ad-materials"
)
AD_MATERIAL_PUBLIC_BASE_URL = os.environ.get(
    "AD_MATERIAL_PUBLIC_BASE_URL", "https://ai.yingliangads.com/ad-materials"
)
AD_MATERIAL_ADMIN_URL = os.environ.get("AD_MATERIAL_ADMIN_URL", "https://ai.yingliangads.com/#adMaterials").strip()
AD_MATERIAL_SOURCE_API_URL = os.environ.get(
    "AD_MATERIAL_SOURCE_API_URL", "https://aa.yingliangads.com/api/material/source"
).strip()
AD_MATERIAL_SOURCE_API_TOKEN = os.environ.get("AD_MATERIAL_SOURCE_API_TOKEN", "").strip()
AD_MATERIAL_SOURCE_API_TIMEOUT = int(os.environ.get("AD_MATERIAL_SOURCE_API_TIMEOUT", "30"))
AD_MATERIAL_REQUIREMENT_COMMAND = os.environ.get("AD_MATERIAL_REQUIREMENT_COMMAND", "").strip()
AD_MATERIAL_GENERATION_COMMAND = os.environ.get("AD_MATERIAL_GENERATION_COMMAND", "").strip()
AD_MATERIAL_COMMAND_TIMEOUT = int(os.environ.get("AD_MATERIAL_COMMAND_TIMEOUT", "1800"))
AD_MATERIAL_FINAL_USER_ID = int(os.environ.get("AD_MATERIAL_FINAL_USER_ID", "248"))
AD_MATERIAL_COMPETITOR_ALERT_RECEIVE_ID_TYPE = os.environ.get("AD_MATERIAL_COMPETITOR_ALERT_RECEIVE_ID_TYPE", "").strip()
AD_MATERIAL_COMPETITOR_ALERT_RECEIVE_ID = os.environ.get("AD_MATERIAL_COMPETITOR_ALERT_RECEIVE_ID", "").strip()
AD_MATERIAL_COMPETITOR_ALERT_OPEN_IDS = [
    item.strip() for item in os.environ.get("AD_MATERIAL_COMPETITOR_ALERT_OPEN_IDS", "").split(",") if item.strip()
]


AI_SOURCE_CALLBACK_URL = os.environ.get(
    "AI_SOURCE_CALLBACK_URL", "https://aa.yingliangads.com/api/material/ai-source"
).strip()
AI_SOURCE_CALLBACK_TOKEN = os.environ.get(
    "AI_SOURCE_CALLBACK_TOKEN", ""
).strip()
AI_SOURCE_CALLBACK_ENABLED = os.environ.get("AI_SOURCE_CALLBACK_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
AI_SOURCE_CALLBACK_TIMEOUT = int(os.environ.get("AI_SOURCE_CALLBACK_TIMEOUT", "30"))
ADMIN_MAPPING_MYSQL_HOST = os.environ.get("ADMIN_MAPPING_MYSQL_HOST", "").strip()
ADMIN_MAPPING_MYSQL_PORT = os.environ.get("ADMIN_MAPPING_MYSQL_PORT", "").strip()
ADMIN_MAPPING_MYSQL_USER = os.environ.get("ADMIN_MAPPING_MYSQL_USER", "").strip()
ADMIN_MAPPING_MYSQL_PASSWORD = os.environ.get("ADMIN_MAPPING_MYSQL_PASSWORD", "")
ADMIN_MAPPING_MYSQL_DATABASE = os.environ.get("ADMIN_MAPPING_MYSQL_DATABASE", "").strip()
ADMIN_MAPPING_MYSQL_TIMEOUT = int(os.environ.get("ADMIN_MAPPING_MYSQL_TIMEOUT", "8"))

COS_SECRET_ID = os.environ.get("COS_SECRET_ID", "").strip()

COS_SECRET_KEY = os.environ.get("COS_SECRET_KEY", "").strip()

COS_BUCKET = os.environ.get("COS_BUCKET", "").strip()

COS_REGION = os.environ.get("COS_REGION", "").strip()

COS_DOMAIN = os.environ.get("COS_DOMAIN", "").strip()

COS_PREFIX = os.environ.get("COS_PREFIX", "drama-materials").strip().strip("/")
COS_UPLOAD_TIMEOUT = int(os.environ.get("COS_UPLOAD_TIMEOUT", "120"))
COS_MULTIPART_THRESHOLD = int(os.environ.get("COS_MULTIPART_THRESHOLD", str(64 * 1024 * 1024)))
COS_MULTIPART_PART_SIZE_MB = int(os.environ.get("COS_MULTIPART_PART_SIZE_MB", "16"))
COS_MULTIPART_THREADS = int(os.environ.get("COS_MULTIPART_THREADS", "8"))
COS_MULTIPART_TIMEOUT = int(os.environ.get("COS_MULTIPART_TIMEOUT", "900"))





























FFMPEG = os.environ.get("DRAMA_FFMPEG", "/root/ffmpeg-static/ffmpeg")
FFMPEG_VIDEO_ENCODER = os.environ.get("DRAMA_VIDEO_ENCODER", "auto").strip().lower()
FFMPEG_NVENC_PRESET = os.environ.get("DRAMA_NVENC_PRESET", "fast").strip()
FFMPEG_NVENC_CQ = os.environ.get("DRAMA_NVENC_CQ", "23").strip()
FFMPEG_FILTER_BACKEND = os.environ.get("DRAMA_FILTER_BACKEND", "auto").strip().lower()
_FFMPEG_ENCODER_CACHE = {}
_FFMPEG_FILTER_CACHE = {}
































INTRO_SECONDS = int(os.environ.get("DRAMA_INTRO_SECONDS", "1"))































DOWNLOAD_TIMEOUT = int(os.environ.get("DRAMA_DOWNLOAD_TIMEOUT", "180"))































CODEX_COVER_SERVICE_URL = os.environ.get(































    "CODEX_COVER_SERVICE_URL", "http://127.0.0.1:8790/api/codex-cover/generate"































)































CODEX_COVER_SERVICE_TIMEOUT = int(os.environ.get("CODEX_COVER_SERVICE_TIMEOUT", "1200"))

CODEX_SCREENSHOT_SERVICE_URL = os.environ.get(

    "CODEX_SCREENSHOT_SERVICE_URL", "http://127.0.0.1:8791/api/codex-screenshot/generate"

)

CODEX_SCREENSHOT_SERVICE_TIMEOUT = int(os.environ.get("CODEX_SCREENSHOT_SERVICE_TIMEOUT", "1800"))

CODEX_SCREENSHOT_SERVICE_URLS = {
    "square_1x1": os.environ.get("CODEX_SCREENSHOT_SERVICE_URL_SQUARE_1X1", "http://127.0.0.1:8792/api/codex-screenshot/generate"),
    "landscape_1_91x1": os.environ.get("CODEX_SCREENSHOT_SERVICE_URL_LANDSCAPE_1_91X1", "http://127.0.0.1:8793/api/codex-screenshot/generate"),
    "portrait_4x5": os.environ.get("CODEX_SCREENSHOT_SERVICE_URL_PORTRAIT_4X5", "http://127.0.0.1:8794/api/codex-screenshot/generate"),
}

def parse_env_csv(name):
    return [
        item.strip()
        for item in str(os.environ.get(name, "") or "").replace("\n", ",").split(",")
        if item.strip()
    ]


SCREENSHOT_JOB_BASE_CONCURRENCY = max(1, int(os.environ.get("SCREENSHOT_JOB_MAX_CONCURRENCY", "1")))
SCREENSHOT_JOB_BURST_QUEUE_THRESHOLD = max(1, int(os.environ.get("SCREENSHOT_JOB_BURST_QUEUE_THRESHOLD", "2")))
SCREENSHOT_JOB_BURST_CONCURRENCY = max(
    SCREENSHOT_JOB_BASE_CONCURRENCY,
    int(os.environ.get("SCREENSHOT_JOB_BURST_CONCURRENCY", str(SCREENSHOT_JOB_BASE_CONCURRENCY))),
)
CODEX_SCREENSHOT_SERVICE_POOL = parse_env_csv("CODEX_SCREENSHOT_SERVICE_POOL")
CODEX_SCREENSHOT_SERVICE_POOL_BURST_ONLY = os.environ.get(
    "CODEX_SCREENSHOT_SERVICE_POOL_BURST_ONLY", "1"
).strip().lower() in ("1", "true", "yes", "on")
SCREENSHOT_JOB_ACTIVE_COUNT = 0
SCREENSHOT_JOB_CONDITION = threading.Condition()
SCREENSHOT_SERVICE_POOL_INDEX = 0
SCREENSHOT_SERVICE_POOL_LOCK = threading.Lock()
SCREENSHOT_SERVICE_POOL_INFLIGHT = {}





























CODEX_MEDIA_WORKSPACE = os.environ.get(































    "CODEX_MEDIA_WORKSPACE", "/root/codex_media_worker_workspace"































)































DEMUCS_PYTHON = os.environ.get(































    "DEMUCS_PYTHON", "/root/miniconda3/envs/drama-voice/bin/python"































)































DEMUCS_SCRIPT = os.environ.get(































    "DEMUCS_SCRIPT", "/root/drama_material_service/demucs_extract_vocals.py"































)































DEMUCS_MODEL = os.environ.get("DEMUCS_MODEL", "htdemucs")































DEMUCS_FALLBACK_MODEL = os.environ.get("DEMUCS_FALLBACK_MODEL", "mdx_extra_q")































DEMUCS_DEVICE = os.environ.get("DEMUCS_DEVICE", "cpu")































DEMUCS_SEGMENT = int(os.environ.get("DEMUCS_SEGMENT", "8"))































DEMUCS_FALLBACK_SEGMENT = int(os.environ.get("DEMUCS_FALLBACK_SEGMENT", "6"))































DEMUCS_SHIFTS = int(os.environ.get("DEMUCS_SHIFTS", "1"))































DEMUCS_FALLBACK_SHIFTS = int(os.environ.get("DEMUCS_FALLBACK_SHIFTS", "1"))































DEMUCS_JOBS = int(os.environ.get("DEMUCS_JOBS", "0"))































DEMUCS_CHUNK_SECONDS = int(os.environ.get("DEMUCS_CHUNK_SECONDS", "90"))































DEMUCS_FALLBACK_CHUNK_SECONDS = int(os.environ.get("DEMUCS_FALLBACK_CHUNK_SECONDS", "45"))































JOB_AUTO_RETRY_ATTEMPTS = int(os.environ.get("JOB_AUTO_RETRY_ATTEMPTS", "1"))
DRAMA_JOB_USE_WORKER = os.environ.get("DRAMA_JOB_USE_WORKER", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
DRAMA_PUBLIC_ARTIFACT_CHECK_TIMEOUT = int(os.environ.get("DRAMA_PUBLIC_ARTIFACT_CHECK_TIMEOUT", "20"))
GPU_VIDEO_RESULT_ROOT = os.environ.get("GPU_VIDEO_RESULT_ROOT", "/root/drama_material_job_results")
SCREENSHOT_ITEM_RETRY_ATTEMPTS = int(os.environ.get("SCREENSHOT_ITEM_RETRY_ATTEMPTS", "3"))
CODEX_SCREENSHOT_BATCH_ENABLED = os.environ.get("CODEX_SCREENSHOT_BATCH_ENABLED", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)
CODEX_SCREENSHOT_BATCH_STRICT = os.environ.get("CODEX_SCREENSHOT_BATCH_STRICT", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)































DEMUCS_TIMEOUT = int(os.environ.get("DEMUCS_TIMEOUT", "3600"))

GPU_VIDEO_WORKER_URL = os.environ.get("GPU_VIDEO_WORKER_URL", "").strip().rstrip("/")
GPU_VIDEO_WORKER_TOKEN = os.environ.get("GPU_VIDEO_WORKER_TOKEN", "").strip()
GPU_VIDEO_WORKER_TIMEOUT = int(os.environ.get("GPU_VIDEO_WORKER_TIMEOUT", "14400"))































































logging.basicConfig(































    level=logging.INFO,































    format="%(asctime)s %(levelname)s %(threadName)s %(message)s",































)































JOB_DB_LOCK = threading.Lock()































DEMUCS_LOCK = threading.Lock()

JOB_RETRY_LOCKS_LOCK = threading.Lock()

JOB_RETRY_LOCKS = {}

GPU_VIDEO_RENDER_LOCKS_LOCK = threading.Lock()

GPU_VIDEO_RENDER_LOCKS = {}































PRODUCT_CACHE_LOCK = threading.Lock()































PRODUCT_CACHE = {"items": [], "updated_at": 0}































PRODUCTS_FILE = os.environ.get(































    "DRAMA_PRODUCTS_FILE", "/root/drama_material_service/products.json"































)































SITE_BASE_URL = os.environ.get("DRAMA_SITE_BASE_URL", "https://ai.yingliangads.com").rstrip("/")































FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()































FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()































FEISHU_REDIRECT_URI = os.environ.get(































    "FEISHU_REDIRECT_URI", SITE_BASE_URL + "/api/auth/feishu/callback"































).strip()































FEISHU_SCOPE = os.environ.get(
    "FEISHU_SCOPE", "contact:user.id:readonly contact:user.email:readonly"
).strip()































FEISHU_APP_ACCESS_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"















FEISHU_TENANT_ACCESS_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"















FEISHU_USER_ACCESS_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/access_token"















FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"















FEISHU_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"















FEISHU_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"















SESSION_TTL_SECONDS = int(os.environ.get("DRAMA_SESSION_TTL_SECONDS", str(7 * 24 * 3600)))































SESSION_COOKIE_NAME = os.environ.get("DRAMA_SESSION_COOKIE_NAME", "drama_admin_session")































AUTH_STATE_TTL_SECONDS = int(os.environ.get("DRAMA_AUTH_STATE_TTL_SECONDS", "900"))































DEFAULT_ALLOWED_TENANT_KEYS = "149bb4a14b57975f"































DEFAULT_ADMIN_USER_IDS = "892fd2e8"































DEFAULT_ADMIN_NAMES = "郜远"































FEISHU_ALLOWED_TENANT_KEYS = [item.strip() for item in os.environ.get("FEISHU_ALLOWED_TENANT_KEYS", DEFAULT_ALLOWED_TENANT_KEYS).split(",") if item.strip()]































ADMIN_USER_IDS = [item.strip() for item in os.environ.get("DRAMA_ADMIN_USER_IDS", DEFAULT_ADMIN_USER_IDS).split(",") if item.strip()]































ADMIN_NAMES = [item.strip() for item in os.environ.get("DRAMA_ADMIN_NAMES", DEFAULT_ADMIN_NAMES).split(",") if item.strip()]































MODULE_PERMISSIONS = {















    "drama_synthesis": "剧集合成",

    "cover_synthesis": "封面图合成",

    "ad_material_tasks": "投放素材任务",

    "ad_control_center": "产品广告调控中心",
    "voiceover_drama_tasks": "配音剧语种任务",















    "settings": "设置",















}















DEFAULT_USER_PERMISSIONS = {
    "drama_synthesis": False,
    "cover_synthesis": False,
    "ad_material_tasks": False,
    "ad_control_center": False,
    "voiceover_drama_tasks": False,
    "settings": False,
}















ADMIN_PERMISSIONS = {key: True for key in MODULE_PERMISSIONS}

SCREENSHOT_API_TOKEN_NAME = os.environ.get("DRAMA_SCREENSHOT_API_TOKEN_NAME", "screenshot-api").strip() or "screenshot-api"

SCREENSHOT_API_TOKENS = []

for _token_value in (
    os.environ.get("DRAMA_SCREENSHOT_API_TOKEN", ""),
    os.environ.get("AI_COVER_API_TOKEN", ""),
    os.environ.get("DRAMA_SCREENSHOT_API_TOKENS", ""),
):
    for _token_item in str(_token_value or "").split(","):
        _token_item = _token_item.strip()
        if _token_item and _token_item not in SCREENSHOT_API_TOKENS:
            SCREENSHOT_API_TOKENS.append(_token_item)































AUTH_CACHE_LOCK = threading.Lock()































FEISHU_APP_ACCESS_TOKEN_CACHE = {"token": "", "expires_at": 0}















FEISHU_TENANT_ACCESS_TOKEN_CACHE = {"token": "", "expires_at": 0}















FEISHU_LOGIN_STATES = {}















































JOB_TABLE_SQL = """































CREATE TABLE IF NOT EXISTS drama_material_job (































  id INTEGER PRIMARY KEY AUTOINCREMENT,































  job_id TEXT NOT NULL UNIQUE,































  app_id TEXT NOT NULL DEFAULT '',































  content_id TEXT NOT NULL,































  app TEXT NOT NULL DEFAULT '',































  country TEXT NOT NULL DEFAULT '',































  language TEXT NOT NULL DEFAULT '',































  drama_name TEXT NOT NULL DEFAULT '',































  episode_start INTEGER NOT NULL DEFAULT 0,































  episode_end INTEGER NOT NULL DEFAULT 0,































  total_episodes INTEGER NOT NULL DEFAULT 0,































  cover_source_url TEXT NOT NULL DEFAULT '',































  cover_16x9_url TEXT NOT NULL DEFAULT '',































  output_video_url TEXT NOT NULL DEFAULT '',































  output_video_no_bgm_url TEXT NOT NULL DEFAULT '',































  outputs_json TEXT NOT NULL DEFAULT '{}',































  advanced_options_json TEXT NOT NULL DEFAULT '{}',































  status TEXT NOT NULL DEFAULT 'queued',































  progress INTEGER NOT NULL DEFAULT 0,































  progress_detail TEXT NOT NULL DEFAULT '',















  error_message TEXT NOT NULL DEFAULT '',















  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,















  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

  finished_at TEXT NOT NULL DEFAULT '',















  creator_user_id TEXT NOT NULL DEFAULT '',















  creator_open_id TEXT NOT NULL DEFAULT '',















  creator_name TEXT NOT NULL DEFAULT '',















  completion_notified_at TEXT NOT NULL DEFAULT '',















  completion_notification_error TEXT NOT NULL DEFAULT ''















)















"""















































JOB_TABLE_COLUMNS = [































    "job_id",































    "app_id",































    "content_id",































    "app",































    "country",































    "language",































    "drama_name",































    "episode_start",































    "episode_end",































    "total_episodes",































    "cover_source_url",































    "cover_16x9_url",































    "output_video_url",































    "output_video_no_bgm_url",































    "outputs_json",































    "advanced_options_json",































    "status",































    "progress",































    "progress_detail",































    "error_message",















    "created_at",















    "updated_at",

    "finished_at",















    "creator_user_id",















    "creator_open_id",















    "creator_name",















    "completion_notified_at",















    "completion_notification_error",















]















































SCREENSHOT_JOB_TABLE_SQL = """

CREATE TABLE IF NOT EXISTS drama_screenshot_job (

  id INTEGER PRIMARY KEY AUTOINCREMENT,

  job_id TEXT NOT NULL UNIQUE,

  app_id TEXT NOT NULL DEFAULT '',

  content_id TEXT NOT NULL,

  app TEXT NOT NULL DEFAULT '',

  country TEXT NOT NULL DEFAULT '',

  language TEXT NOT NULL DEFAULT '',

  drama_name TEXT NOT NULL DEFAULT '',

  cover_source_url TEXT NOT NULL DEFAULT '',

  square_1x1_url TEXT NOT NULL DEFAULT '',

  landscape_1_91x1_url TEXT NOT NULL DEFAULT '',

  portrait_4x5_url TEXT NOT NULL DEFAULT '',

  assets_json TEXT NOT NULL DEFAULT '{}',

  status TEXT NOT NULL DEFAULT 'queued',

  progress INTEGER NOT NULL DEFAULT 0,

  progress_detail TEXT NOT NULL DEFAULT '',

  error_message TEXT NOT NULL DEFAULT '',

  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

  started_at TEXT NOT NULL DEFAULT '',

  finished_at TEXT NOT NULL DEFAULT '',

  elapsed_seconds INTEGER NOT NULL DEFAULT 0,

  token_total INTEGER NOT NULL DEFAULT 0,

  token_usage_json TEXT NOT NULL DEFAULT '{}',

  creator_user_id TEXT NOT NULL DEFAULT '',

  creator_open_id TEXT NOT NULL DEFAULT '',

  creator_name TEXT NOT NULL DEFAULT ''

)

"""





SCREENSHOT_JOB_TABLE_COLUMNS = [

    "job_id",

    "app_id",

    "content_id",

    "app",

    "country",

    "language",

    "drama_name",

    "cover_source_url",

    "square_1x1_url",

    "landscape_1_91x1_url",

    "portrait_4x5_url",

    "assets_json",

    "status",

    "progress",

    "progress_detail",

    "error_message",

    "created_at",

    "updated_at",

    "started_at",

    "finished_at",

    "elapsed_seconds",

    "token_total",

    "token_usage_json",

    "creator_user_id",

    "creator_open_id",

    "creator_name",

]





SCREENSHOT_SPECS = [

    {

        "key": "square_1x1",

        "field": "square_1x1_url",

        "label": "1:1 方图",

        "ratio": "1:1",

        "width": 1200,

        "height": 1200,

        "filename": "square_1x1.jpg",

    },

    {

        "key": "landscape_1_91x1",

        "field": "landscape_1_91x1_url",

        "label": "1.91:1 横图",

        "ratio": "1.91:1",

        "width": 1200,

        "height": 628,

        "filename": "landscape_1_91x1.jpg",

    },

    {

        "key": "portrait_4x5",

        "field": "portrait_4x5_url",

        "label": "4:5 竖图",

        "ratio": "4:5",

        "width": 1200,

        "height": 1500,

        "filename": "portrait_4x5.jpg",

    },

]





AUTH_SESSION_TABLE_SQL = """





























CREATE TABLE IF NOT EXISTS drama_admin_session (































  id INTEGER PRIMARY KEY AUTOINCREMENT,































  session_token TEXT NOT NULL UNIQUE,































  user_id TEXT NOT NULL DEFAULT '',































  union_id TEXT NOT NULL DEFAULT '',































  open_id TEXT NOT NULL DEFAULT '',































  name TEXT NOT NULL DEFAULT '',































  en_name TEXT NOT NULL DEFAULT '',































  avatar_url TEXT NOT NULL DEFAULT '',































  tenant_key TEXT NOT NULL DEFAULT '',































  source TEXT NOT NULL DEFAULT '',































  expires_at INTEGER NOT NULL DEFAULT 0,































  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,































  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP































)































"""































































USER_TABLE_SQL = """































CREATE TABLE IF NOT EXISTS drama_admin_user (































  id INTEGER PRIMARY KEY AUTOINCREMENT,































  user_id TEXT NOT NULL UNIQUE,































  union_id TEXT NOT NULL DEFAULT '',































  open_id TEXT NOT NULL DEFAULT '',































  name TEXT NOT NULL DEFAULT '',































  en_name TEXT NOT NULL DEFAULT '',































  avatar_url TEXT NOT NULL DEFAULT '',































  tenant_key TEXT NOT NULL DEFAULT '',































  role TEXT NOT NULL DEFAULT 'user',































  permissions_json TEXT NOT NULL DEFAULT '{}',































  status TEXT NOT NULL DEFAULT 'active',































  last_source TEXT NOT NULL DEFAULT '',































  login_count INTEGER NOT NULL DEFAULT 0,































  first_login_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,































  last_login_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,































  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,































  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP































)































"""































































AUDIT_LOG_TABLE_SQL = """































CREATE TABLE IF NOT EXISTS drama_admin_audit_log (































  id INTEGER PRIMARY KEY AUTOINCREMENT,































  actor_user_id TEXT NOT NULL DEFAULT '',































  actor_name TEXT NOT NULL DEFAULT '',































  action TEXT NOT NULL DEFAULT '',































  target_type TEXT NOT NULL DEFAULT '',































  target_id TEXT NOT NULL DEFAULT '',































  detail_json TEXT NOT NULL DEFAULT '{}',































  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP































)































"""































































STATUS_PROGRESS = {































    "queued": 5,































    "validating": 10,































    "downloading": 25,































    "processing_cover": 45,

    "validation_remaking": 45,































    "rendering": 70,































    "removing_bgm": 88,































    "done": 100,































    "failed": 0,































}































































STATUS_LABELS = {































    "queued": "已排队",































    "validating": "校验中",































    "downloading": "拉取源视频",































    "processing_cover": "生成封面",

    "validation_remaking": "校验不通过重新制作中",































    "rendering": "拼接处理",































    "removing_bgm": "去除 BGM",































    "done": "已完成",































    "failed": "失败",































}































































































def feishu_auth_enabled():































    return bool(FEISHU_APP_ID and FEISHU_APP_SECRET)































































































def default_role_for_user(user_info):































    user_id = str(user_info.get("user_id", "") or "")































    name = str(user_info.get("name", "") or "")































    if user_id in ADMIN_USER_IDS or name in ADMIN_NAMES:































        return "admin"































    return "user"































































































































def normalize_user_permissions(value, role="user"):































    if role == "admin":































        return dict(ADMIN_PERMISSIONS)































    permissions = dict(DEFAULT_USER_PERMISSIONS)































    if isinstance(value, str):































        value = parse_json_text(value, {})































    if isinstance(value, dict):































        for key in MODULE_PERMISSIONS:































            if key in value:































                permissions[key] = bool(value.get(key))































    permissions["settings"] = False































    return permissions















































def has_module_permission(session, module_key):































    if not session:































        return False































    if session.get("role") == "admin":































        return True































    permissions = normalize_user_permissions(session.get("permissions", {}), session.get("role", "user"))































    return bool(permissions.get(module_key))















































def assert_feishu_user_allowed(user_info):































    tenant_key = str(user_info.get("tenant_key", "") or "").strip()































    if FEISHU_ALLOWED_TENANT_KEYS and tenant_key not in FEISHU_ALLOWED_TENANT_KEYS:































        raise PermissionError("仅允许盈量团队成员登录")































    return True































































































def now_text():































    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def utc_now_text():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")































































































def file_ready(path):































    return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0


def image_file_ready(path):
    if not file_ready(path):
        return False
    try:
        from PIL import Image as PilImage

        with PilImage.open(path) as image:
            image.verify()
        with PilImage.open(path) as image:
            image.load()
        return True
    except Exception:
        return False


def remove_file_quietly(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        logging.warning("failed to remove file: %s", path)


def get_named_runtime_lock(lock_map, lock_guard, key):
    key = str(key or "").strip()
    with lock_guard:
        lock = lock_map.get(key)
        if lock is None:
            lock = threading.Lock()
            lock_map[key] = lock
        return lock


def valid_video_file(path, min_duration=0.5):
    if not file_ready(path):
        return False
    try:
        proc = subprocess.run(
            [
                ffprobe_path(), "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_type:format=duration",
                "-of", "json",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return False
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return False
        duration = float((data.get("format") or {}).get("duration") or 0)
        return duration >= float(min_duration or 0)
    except Exception as exc:
        logging.warning("failed to validate video file %s: %s", path, exc)
        return False


def probe_media_stream_info(path):
    if not file_ready(path):
        return {}
    try:
        proc = subprocess.run(
            [
                ffprobe_path(), "-v", "error",
                "-show_entries",
                "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,time_base,sample_rate,channels,duration:format=duration",
                "-of", "json",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=60,
        )
        if proc.returncode != 0:
            logging.warning("failed to probe media streams for %s: %s", path, proc.stderr.strip())
            return {}
        return json.loads(proc.stdout or "{}")
    except Exception as exc:
        logging.warning("failed to probe media streams for %s: %s", path, exc)
        return {}


def media_duration_delta_seconds(path):
    data = probe_media_stream_info(path)
    streams = data.get("streams") or []
    video_duration = None
    audio_duration = None
    for stream in streams:
        codec_type = stream.get("codec_type")
        try:
            duration = float(stream.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration <= 0:
            continue
        if codec_type == "video" and video_duration is None:
            video_duration = duration
        elif codec_type == "audio" and audio_duration is None:
            audio_duration = duration
    if video_duration is None or audio_duration is None:
        return None
    return abs(video_duration - audio_duration)


def valid_av_duration_alignment(path, max_delta=1.0):
    delta = media_duration_delta_seconds(path)
    if delta is None:
        return True
    return delta <= float(max_delta)


def remove_invalid_video_file(path, label="video"):
    if not path or not os.path.exists(path):
        return False
    if valid_video_file(path) and valid_av_duration_alignment(path):
        return False
    try:
        os.remove(path)
        logging.warning("removed invalid %s file: %s", label, path)
        return True
    except OSError as exc:
        logging.warning("failed to remove invalid %s file %s: %s", label, path, exc)
        return False































































































def status_rank(status):































    order = {































        "done": 7,































        "removing_bgm": 6,































        "rendering": 5,































        "processing_cover": 4,































        "downloading": 3,































        "validating": 2,































        "queued": 1,































        "failed": 0,































    }





def parse_job_timestamp(value):
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("T", " ")[:19]
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def nonnegative_int(value, default=0):
    try:
        number = int(float(value or 0))
    except (TypeError, ValueError):
        return int(default or 0)
    return max(0, number)


def enrich_screenshot_job_timing(job):
    job_id = str(job.get("job_id", "") or "")
    start_text = str(job.get("started_at") or job.get("created_at") or "")
    if job_id and not str(job.get("started_at") or "").strip():
        with JOB_DB_LOCK:
            conn = get_job_db_connection()
            try:
                row = conn.execute(
                    """
                    SELECT created_at
                    FROM drama_admin_audit_log
                    WHERE target_type = 'screenshot_job'
                      AND target_id = ?
                      AND action IN ('retry_screenshot_job', 'create_screenshot_job')
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
            finally:
                conn.close()
        if row and row[0]:
            start_text = row[0]
    end_text = str(job.get("finished_at") or job.get("updated_at") or "")
    job["active_started_at"] = start_text
    job["active_finished_at"] = end_text if job.get("status") in ("done", "failed") else ""
    persisted_seconds = nonnegative_int(job.get("elapsed_seconds"), 0)
    if persisted_seconds and job.get("status") in ("done", "failed"):
        job["active_elapsed_seconds"] = persisted_seconds
        return job
    start_dt = parse_job_timestamp(start_text)
    end_dt = parse_job_timestamp(end_text)
    if start_dt and end_dt and end_dt >= start_dt and job.get("status") in ("done", "failed"):
        job["active_elapsed_seconds"] = int((end_dt - start_dt).total_seconds())
    return job


def enrich_material_job_timing(job):
    job_id = str(job.get("job_id", "") or "")
    start_text = str(job.get("created_at", "") or "")
    if job_id:
        with JOB_DB_LOCK:
            conn = get_job_db_connection()
            try:
                row = conn.execute(
                    """
                    SELECT created_at
                    FROM drama_admin_audit_log
                    WHERE target_type = 'job'
                      AND target_id = ?
                      AND action IN ('retry_job', 'create_job')
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
            finally:
                conn.close()
        if row and row[0]:
            start_text = row[0]
    end_text = str(job.get("finished_at") or job.get("updated_at", "") or "")
    job["active_started_at"] = start_text
    job["active_finished_at"] = end_text if job.get("status") == "done" else ""
    start_dt = parse_job_timestamp(start_text)
    end_dt = parse_job_timestamp(end_text)
    if start_dt and end_dt and end_dt >= start_dt and job.get("status") == "done":
        job["active_elapsed_seconds"] = int((end_dt - start_dt).total_seconds())
    return job


def row_to_screenshot_job(row):
    assets = parse_json_text(row[11], {})
    status = row[12]
    token_usage = parse_json_text(row[22], {}) if len(row) > 22 else {}
    stored_progress = clamp_progress(row[13])
    if stored_progress <= 0 and status != "failed":
        stored_progress = progress_for_status(status)
    progress_detail = (row[14] or "").strip() or default_progress_detail(status)
    if progress_detail and re.fullmatch(r"[?\uFF1F\s]+", progress_detail):
        progress_detail = "\u4e09\u79cd\u622a\u56fe\u7d20\u6750\u5df2\u5168\u90e8\u751f\u6210" if status == "done" else default_progress_detail(status)
    status_text = "\u751f\u6210\u622a\u56fe" if status == "processing_cover" else status_label(status)
    job = {

        "job_id": row[0],

        "app_id": row[1],

        "content_id": row[2],

        "app": product_name_for_app_id(row[1], row[3]),

        "country": row[4],

        "language": row[5],

        "drama_name": row[6],

        "cover_source_url": row[7],

        "square_1x1_url": row[8],

        "landscape_1_91x1_url": row[9],

        "portrait_4x5_url": row[10],

        "assets": assets,

        "status": status,

        "status_label": status_text,

        "progress": stored_progress,

        "progress_detail": progress_detail,

        "error_message": row[15],

        "created_at": row[16],

        "updated_at": row[17],

        "started_at": row[18] if len(row) > 18 else "",

        "finished_at": row[19] if len(row) > 19 else "",

        "elapsed_seconds": nonnegative_int(row[20] if len(row) > 20 else 0, 0),

        "token_total": nonnegative_int(row[21] if len(row) > 21 else 0, 0),

        "token_usage": token_usage if isinstance(token_usage, dict) else {},

        "creator_user_id": row[23] if len(row) > 23 else "",

        "creator_open_id": row[24] if len(row) > 24 else "",

        "creator_name": row[25] if len(row) > 25 else "",

        "result_preview": {

            "square_1x1": row[8],

            "landscape_1_91x1": row[9],

            "portrait_4x5": row[10],

        },

    }
    return enrich_screenshot_job_timing(job)





























    return order.get(status or "", -1)































































































def now_ts():































    return datetime.now().timestamp()































































































def ensure_dir(path):































    if path and not os.path.isdir(path):































        os.makedirs(path)































































































def deleted_marker_path(job_id):































    return os.path.join(WORK_ROOT, ".deleted_%s" % job_id)































































































def mark_job_deleted(job_id):































    ensure_dir(WORK_ROOT)































    with open(deleted_marker_path(job_id), "w") as fh:































        fh.write(now_text())































































































def clear_job_deleted_marker(job_id):































    marker = deleted_marker_path(job_id)































    if os.path.exists(marker):































        os.remove(marker)































































































def is_job_deleted(job_id):































    return os.path.exists(deleted_marker_path(job_id))


def screenshot_deleted_marker_path(job_id):

    return os.path.join(SCREENSHOT_WORK_ROOT, ".deleted_%s" % job_id)


def mark_screenshot_job_deleted(job_id):

    ensure_dir(SCREENSHOT_WORK_ROOT)

    with open(screenshot_deleted_marker_path(job_id), "w") as fh:

        fh.write(now_text())


def clear_screenshot_job_deleted_marker(job_id):

    marker = screenshot_deleted_marker_path(job_id)

    if os.path.exists(marker):

        os.remove(marker)


def is_screenshot_job_deleted(job_id):

    return os.path.exists(screenshot_deleted_marker_path(job_id))































































































def shell_quote(value):































    return value.replace("\\", "\\\\").replace("'", "\\'")































































































def json_response(handler, status_code, payload):































    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")































    handler.send_response(status_code)































    handler.send_header("Content-Type", "application/json; charset=utf-8")































    handler.send_header("Content-Length", str(len(body)))































    handler.end_headers()































    handler.wfile.write(body)


class StructuredApiError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = str(code or "bad_request")
        self.message = str(message or self.code)
        self.details = {
            str(key): value
            for key, value in (details or {}).items()
            if value is not None and value != ""
        }

    def to_payload(self):
        payload = {
            "code": self.code,
        }
        payload.update(self.details)
        payload["error"] = self.message
        payload["message"] = self.message
        return payload


def api_error_payload(exc, default_code="bad_request"):
    if isinstance(exc, StructuredApiError):
        return exc.to_payload()
    message = str(exc).strip() or exc.__class__.__name__
    return {
        "code": default_code,
        "error": message,
        "message": message,
    }































































































def redirect_response(handler, location):































    handler.send_response(302)































    handler.send_header("Location", location)































    handler.send_header("Content-Length", "0")































    handler.end_headers()































































































def set_cookie_header(handler, name, value, max_age=None, path="/", http_only=True):































    morsel = cookies.SimpleCookie()































    morsel[name] = value































    morsel[name]["path"] = path































    if SITE_BASE_URL.startswith("https://"):































        morsel[name]["secure"] = True































    if http_only:































        morsel[name]["httponly"] = True































    if max_age is not None:































        morsel[name]["max-age"] = int(max_age)































    cookie_value = morsel.output(header="").strip()































    if "SameSite=" not in cookie_value:































        cookie_value += "; SameSite=Lax"































    handler.send_header("Set-Cookie", cookie_value)































































































def parse_cookie_header(header_value):































    if not header_value:































        return {}































    jar = cookies.SimpleCookie()































    try:































        jar.load(header_value)































    except Exception:































        return {}































    return {key: morsel.value for key, morsel in jar.items()}































































































def parse_json_text(value, default):































    text = (value or "").strip()































    if not text:































        return default































    try:































        return json.loads(text)































    except Exception:































        return default































































































def normalize_outputs(raw_outputs):































    outputs = raw_outputs if isinstance(raw_outputs, dict) else {}































    normalized = {































        "concat_video": bool(outputs.get("concat_video", True)),































        "no_bgm_video": bool(outputs.get("no_bgm_video", True)),































        "cover_16x9": bool(outputs.get("cover_16x9", True)),































    }































    if not any(normalized.values()):































        raise ValueError("至少选择一个输出项")































    return normalized


def selected_job_outputs_ready(job):
    outputs = normalize_outputs(job.get("outputs", {}))
    if outputs["concat_video"] and not str(job.get("output_video_url") or "").strip():
        return False
    if outputs["no_bgm_video"] and not str(job.get("output_video_no_bgm_url") or "").strip():
        return False
    if outputs["cover_16x9"] and not str(job.get("cover_16x9_url") or "").strip():
        return False
    return True































































































def normalize_advanced_options(raw_options):































    options = raw_options if isinstance(raw_options, dict) else {}































    return {































        "overwrite_existing": bool(options.get("overwrite_existing", False)),































        "cover_template": str(options.get("cover_template", "default") or "default"),































        "naming_rule": str(options.get("naming_rule", "default") or "default"),































        "output_resolution": str(options.get("output_resolution", "1280x720") or "1280x720"),































    }































































































def run_mysql(query):
    mysql_env = os.environ.copy()
    if MYSQL_PASSWORD:
        mysql_env["MYSQL_PWD"] = MYSQL_PASSWORD































    proc = subprocess.run(































        MYSQL_BASE_CMD + [query],































        check=True,































        stdout=subprocess.PIPE,































        stderr=subprocess.PIPE,































        universal_newlines=True,

        env=mysql_env,































    )































    rows = []































    for line in proc.stdout.splitlines():































        if line.strip():































            rows.append(line.split("\t"))































    return rows































































































def get_job_db_connection():































    ensure_dir(os.path.dirname(JOB_DB_PATH))































    conn = sqlite3.connect(JOB_DB_PATH)































    conn.row_factory = sqlite3.Row































    return conn































































































def rebuild_job_table(conn, columns):































    legacy_columns = set(columns)































    conn.execute("ALTER TABLE drama_material_job RENAME TO drama_material_job_legacy")































    conn.execute(JOB_TABLE_SQL)































    select_exprs = []































    for column in JOB_TABLE_COLUMNS:































        if column in legacy_columns:































            select_exprs.append(column)































        elif column in ("outputs_json", "advanced_options_json"):































            select_exprs.append("'{}' AS %s" % column)































        else:































            select_exprs.append("'' AS %s" % column)































    conn.execute(































        """































        INSERT INTO drama_material_job ({columns})































        SELECT {select_exprs}































        FROM drama_material_job_legacy































        """.format(































            columns=", ".join(JOB_TABLE_COLUMNS),































            select_exprs=", ".join(select_exprs),































        )































    )































    conn.execute("DROP TABLE drama_material_job_legacy")































































































def ensure_job_table():































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            conn.execute(JOB_TABLE_SQL)































            columns = [































                row["name"]































                for row in conn.execute("PRAGMA table_info(drama_material_job)").fetchall()































            ]































            expected = ["id"] + JOB_TABLE_COLUMNS































            if columns != expected:































                rebuild_job_table(conn, columns)































            conn.execute(































                "CREATE INDEX IF NOT EXISTS idx_drama_material_job_content_id ON drama_material_job(content_id)"































            )































            conn.execute(































                "CREATE INDEX IF NOT EXISTS idx_drama_material_job_app_id_content_id ON drama_material_job(app_id, content_id)"































            )































            conn.execute(































                "CREATE INDEX IF NOT EXISTS idx_drama_material_job_status_updated_at ON drama_material_job(status, updated_at)"































            )































            conn.commit()































        finally:































            conn.close()































































































def rebuild_screenshot_job_table(conn, columns):

    legacy_columns = set(columns)

    conn.execute("ALTER TABLE drama_screenshot_job RENAME TO drama_screenshot_job_legacy")

    conn.execute(SCREENSHOT_JOB_TABLE_SQL)

    select_exprs = []

    for column in SCREENSHOT_JOB_TABLE_COLUMNS:

        if column in legacy_columns:

            select_exprs.append(column)

        elif column in ("assets_json", "token_usage_json"):

            select_exprs.append("'{}' AS %s" % column)

        elif column in ("elapsed_seconds", "token_total"):

            select_exprs.append("0 AS %s" % column)

        else:

            select_exprs.append("'' AS %s" % column)

    conn.execute(

        """

        INSERT INTO drama_screenshot_job ({columns})

        SELECT {select_exprs}

        FROM drama_screenshot_job_legacy

        """.format(

            columns=", ".join(SCREENSHOT_JOB_TABLE_COLUMNS),

            select_exprs=", ".join(select_exprs),

        )

    )

    conn.execute("DROP TABLE drama_screenshot_job_legacy")





def ensure_screenshot_job_table():

    with JOB_DB_LOCK:

        conn = get_job_db_connection()

        try:

            conn.execute(SCREENSHOT_JOB_TABLE_SQL)

            columns = [

                row["name"]

                for row in conn.execute("PRAGMA table_info(drama_screenshot_job)").fetchall()

            ]

            expected = ["id"] + SCREENSHOT_JOB_TABLE_COLUMNS

            if columns != expected:

                rebuild_screenshot_job_table(conn, columns)

            conn.execute(

                "CREATE INDEX IF NOT EXISTS idx_drama_screenshot_job_app_id_content_id ON drama_screenshot_job(app_id, content_id)"

            )

            conn.execute(

                "CREATE INDEX IF NOT EXISTS idx_drama_screenshot_job_status_updated_at ON drama_screenshot_job(status, updated_at)"

            )

            conn.commit()

        finally:

            conn.close()





def ensure_auth_session_table():





























    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            conn.execute(AUTH_SESSION_TABLE_SQL)































            conn.execute(































                "CREATE INDEX IF NOT EXISTS idx_drama_admin_session_expires_at ON drama_admin_session(expires_at)"































            )































            conn.commit()































        finally:































            conn.close()































































































def ensure_user_table():































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            conn.execute(USER_TABLE_SQL)































            columns = [row["name"] for row in conn.execute("PRAGMA table_info(drama_admin_user)").fetchall()]















            if "permissions_json" not in columns:















                conn.execute("ALTER TABLE drama_admin_user ADD COLUMN permissions_json TEXT NOT NULL DEFAULT '{}'")















            conn.execute(















                """















                UPDATE drama_admin_user















                SET permissions_json = CASE















                  WHEN role = 'admin' THEN ?















                  WHEN TRIM(permissions_json) = '' OR permissions_json = '{}' THEN ?















                  ELSE permissions_json















                END,















                updated_at = CURRENT_TIMESTAMP















                """,















                (















                    json.dumps(ADMIN_PERMISSIONS, ensure_ascii=False),















                    json.dumps(DEFAULT_USER_PERMISSIONS, ensure_ascii=False),















                ),















            )































            conn.execute(































                "CREATE INDEX IF NOT EXISTS idx_drama_admin_user_role ON drama_admin_user(role)"































            )































            conn.execute(































                "CREATE INDEX IF NOT EXISTS idx_drama_admin_user_tenant_key ON drama_admin_user(tenant_key)"































            )































            conn.commit()































        finally:































            conn.close()































































































def ensure_audit_log_table():































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            conn.execute(AUDIT_LOG_TABLE_SQL)































            conn.execute(































                "CREATE INDEX IF NOT EXISTS idx_drama_admin_audit_log_created_at ON drama_admin_audit_log(created_at)"































            )































            conn.execute(































                "CREATE INDEX IF NOT EXISTS idx_drama_admin_audit_log_actor ON drama_admin_audit_log(actor_user_id)"































            )































            conn.commit()































        finally:































            conn.close()































































































def backfill_users_from_sessions():































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            rows = conn.execute(































                """































                SELECT user_id, union_id, open_id, name, en_name, avatar_url, tenant_key,































                       source, COUNT(*) AS login_count, MIN(created_at), MAX(updated_at)































                FROM drama_admin_session































                WHERE TRIM(user_id) != ''































                GROUP BY user_id, union_id, open_id, name, en_name, avatar_url, tenant_key, source































                """































            ).fetchall()































            for row in rows:































                role = default_role_for_user({"user_id": row[0], "name": row[3]})































                conn.execute(































                    """































                    INSERT INTO drama_admin_user (































                      user_id, union_id, open_id, name, en_name, avatar_url, tenant_key,































                      role, status, last_source, login_count, first_login_at, last_login_at, created_at, updated_at































                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)































                    ON CONFLICT(user_id) DO UPDATE SET































                      union_id=excluded.union_id,































                      open_id=excluded.open_id,































                      name=excluded.name,































                      en_name=excluded.en_name,
































                      avatar_url=excluded.avatar_url,































                      tenant_key=excluded.tenant_key,































                      role=CASE































                        WHEN drama_admin_user.user_id IN ({admin_ids}) OR drama_admin_user.name IN ({admin_names})































                          THEN 'admin'































                        ELSE drama_admin_user.role































                      END,































                      last_source=excluded.last_source,































                      login_count=MAX(drama_admin_user.login_count, excluded.login_count),































                      first_login_at=MIN(drama_admin_user.first_login_at, excluded.first_login_at),































                      last_login_at=MAX(drama_admin_user.last_login_at, excluded.last_login_at),































                      updated_at=CURRENT_TIMESTAMP































                    """.format(































                        admin_ids=", ".join(["'%s'" % item.replace("'", "''") for item in ADMIN_USER_IDS]) or "''",































                        admin_names=", ".join(["'%s'" % item.replace("'", "''") for item in ADMIN_NAMES]) or "''",































                    ),































                    (































                        row[0],































                        row[1],































                        row[2],































                        row[3],































                        row[4],































                        row[5],































                        row[6],































                        role,































                        row[7],































                        int(row[8] or 0),































                        row[9] or now_text(),































                        row[10] or now_text(),































                    ),































                )































            conn.commit()































        finally:































            conn.close()































































































def backfill_audit_logs():































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            has_logs = conn.execute("SELECT COUNT(*) FROM drama_admin_audit_log").fetchone()[0]































            if int(has_logs or 0) > 0:































                return































            rows = conn.execute(































                """































                SELECT session_token, user_id, name, source, tenant_key, created_at































                FROM drama_admin_session































                ORDER BY created_at ASC































                """































            ).fetchall()































            for row in rows:































                conn.execute(































                    """































                    INSERT INTO drama_admin_audit_log (































                      actor_user_id, actor_name, action, target_type, target_id, detail_json, created_at































                    ) VALUES (?, ?, 'login', 'session', ?, ?, ?)































                    """,































                    (































                        row[1] or "",































                        row[2] or "",































                        row[0] or "",































                        json.dumps(































                            {"source": row[3] or "", "tenant_key": row[4] or ""},































                            ensure_ascii=False,































                        ),































                        row[5] or now_text(),































                    ),































                )































            conn.commit()































        finally:































            conn.close()































































































def cleanup_expired_sessions():































    now_epoch = int(time.time())































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            conn.execute("DELETE FROM drama_admin_session WHERE expires_at <= ?", (now_epoch,))































            conn.commit()































        finally:































            conn.close()































































































def recover_inflight_jobs():
    resumable_jobs = []

    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            rows = conn.execute("""
                SELECT
                  job_id, app_id, content_id, app, country, language, drama_name,
                  episode_start, episode_end, total_episodes,
                  cover_source_url, cover_16x9_url, output_video_url,
                  output_video_no_bgm_url, outputs_json, advanced_options_json,
                  status, progress, progress_detail, error_message, created_at, updated_at, finished_at,
                  creator_user_id, creator_open_id, creator_name,
                  completion_notified_at, completion_notification_error
                FROM drama_material_job
                WHERE status NOT IN ('done', 'failed')
            """).fetchall()
            resumable_jobs = [row_to_job(row) for row in rows]
        finally:
            conn.close()

    if DRAMA_JOB_USE_WORKER:
        reconciled_count = 0
        for job in resumable_jobs:
            if reconcile_job_outputs_from_public_artifacts(job, persist=True, notify=True):
                reconciled_count += 1
        if resumable_jobs:
            logging.info(
                "DRAMA_JOB_USE_WORKER=1; skipped API in-process recovery for %d jobs, reconciled %d from public artifacts",
                len(resumable_jobs),
                reconciled_count,
            )
        return

    for job in resumable_jobs:
        logging.info(
            "resuming in-flight drama material job after service restart: %s",
            job.get("job_id"),
        )
        resume_job_from_checkpoint(job)

    return

def recover_inflight_screenshot_jobs():
    resumable_jobs = []
    recovery_progress_detail = "\u670d\u52a1\u91cd\u542f\u540e\u5df2\u91cd\u65b0\u6392\u961f\uff0c\u5c06\u4ece\u5df2\u6709\u4ea7\u7269\u65ad\u70b9\u7ee7\u7eed\u5904\u7406"
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT
                  job_id, app_id, content_id, app, country, language, drama_name,
                  cover_source_url, square_1x1_url, landscape_1_91x1_url, portrait_4x5_url,
                  assets_json, status, progress, progress_detail, error_message, created_at, updated_at,
                  started_at, finished_at, elapsed_seconds, token_total, token_usage_json,
                  creator_user_id, creator_open_id, creator_name
                FROM drama_screenshot_job
                WHERE status NOT IN ('done', 'failed')
                """
            ).fetchall()
            conn.execute(
                """
                UPDATE drama_screenshot_job
                SET status = 'queued',
                    progress = 2,
                    progress_detail = ?,
                    error_message = '',
                    started_at = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status NOT IN ('done', 'failed')
                """,
                (recovery_progress_detail,),
            )
            conn.commit()
            for row in rows:
                resumable_jobs.append(
                    {
                        "job_id": row[0],
                        "app_id": row[1],
                        "content_id": row[2],
                        "app": row[3],
                        "country": row[4],
                        "language": row[5],
                        "drama_name": row[6],
                        "cover_source_url": row[7],
                        "square_1x1_url": row[8],
                        "landscape_1_91x1_url": row[9],
                        "portrait_4x5_url": row[10],
                        "assets": parse_json_text(row[11], {}),
                        "status": "queued",
                        "progress": 2,
                        "progress_detail": recovery_progress_detail,
                        "error_message": "",
                        "created_at": row[16],
                        "updated_at": row[17],
                        "started_at": "",
                        "finished_at": row[19] if len(row) > 19 else "",
                        "elapsed_seconds": nonnegative_int(row[20] if len(row) > 20 else 0, 0),
                        "token_total": nonnegative_int(row[21] if len(row) > 21 else 0, 0),
                        "token_usage": parse_json_text(row[22], {}) if len(row) > 22 else {},
                        "creator_user_id": row[23] if len(row) > 23 else "",
                        "creator_open_id": row[24] if len(row) > 24 else "",
                        "creator_name": row[25] if len(row) > 25 else "",
                    }
                )
        finally:
            conn.close()

    if resumable_jobs:
        logging.info("resuming %d screenshot jobs after service restart", len(resumable_jobs))
    for job in resumable_jobs:
        job["status"] = "queued"
        job["error_message"] = ""
        if not str(job.get("progress_detail", "") or "").strip():
            job["progress_detail"] = recovery_progress_detail
        run_screenshot_job_async(job)






























def build_public_url(path):

    normalized_path = os.path.abspath(path)

    ad_material_root = os.path.abspath(AD_MATERIAL_PUBLIC_ROOT)

    screenshot_root = os.path.abspath(SCREENSHOT_PUBLIC_ROOT)

    default_root = os.path.abspath(PUBLIC_ROOT)

    if normalized_path.startswith(ad_material_root + os.sep) or normalized_path == ad_material_root:

        rel_path = os.path.relpath(normalized_path, ad_material_root).replace(os.sep, "/")

        return AD_MATERIAL_PUBLIC_BASE_URL.rstrip("/") + "/" + rel_path.lstrip("/")

    if normalized_path.startswith(screenshot_root + os.sep) or normalized_path == screenshot_root:

        rel_path = os.path.relpath(normalized_path, screenshot_root).replace(os.sep, "/")

        return SCREENSHOT_PUBLIC_BASE_URL.rstrip("/") + "/" + rel_path.lstrip("/")

    rel_path = os.path.relpath(normalized_path, default_root).replace(os.sep, "/")

    return PUBLIC_BASE_URL.rstrip("/") + "/" + rel_path.lstrip("/")


def build_drama_public_url(job_id, filename):
    public_path = os.path.join(PUBLIC_ROOT, str(job_id).strip("/"), filename.lstrip("/"))
    if cos_enabled():
        return build_cos_url(build_cos_object_key(public_path))
    return build_public_url(public_path)


def public_artifact_ready(url, min_bytes=1):
    url = str(url or "").strip()
    if not url:
        return False
    try:
        response = requests.head(
            url,
            allow_redirects=True,
            timeout=(5, max(5, DRAMA_PUBLIC_ARTIFACT_CHECK_TIMEOUT)),
        )
        if response.status_code not in (200, 206):
            return False
        content_length = response.headers.get("Content-Length")
        if content_length is None:
            return True
        return int(content_length) >= int(min_bytes or 1)
    except Exception as exc:
        logging.warning("public artifact check failed: %s %s", url, exc)
        return False


def reconcile_job_outputs_from_public_artifacts(job, persist=True, notify=False):
    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
        return False
    outputs = normalize_outputs(job.get("outputs", {}))
    candidates = {}
    if outputs["cover_16x9"]:
        candidates["cover_16x9_url"] = str(job.get("cover_16x9_url") or "").strip() or build_drama_public_url(
            job_id, "cover_16x9.jpg"
        )
    if outputs["concat_video"]:
        candidates["output_video_url"] = str(job.get("output_video_url") or "").strip() or build_drama_public_url(
            job_id, "material.mp4"
        )
    if outputs["no_bgm_video"]:
        candidates["output_video_no_bgm_url"] = str(
            job.get("output_video_no_bgm_url") or ""
        ).strip() or build_drama_public_url(job_id, "material_no_bgm.mp4")

    min_bytes = {
        "cover_16x9_url": 1024,
        "output_video_url": 1024 * 1024,
        "output_video_no_bgm_url": 1024 * 1024,
    }
    for key, url in candidates.items():
        if not public_artifact_ready(url, min_bytes.get(key, 1)):
            return False

    for key, url in candidates.items():
        job[key] = url
    job["status"] = "done"
    job["progress"] = 100
    job["progress_detail"] = "\u5168\u90e8\u4ea7\u7269\u5df2\u751f\u6210"
    job["error_message"] = ""
    if persist:
        upsert_job_record(job)
    if notify:
        notify_job_creator_on_completion(job)
    logging.info("reconciled drama job from public artifacts: %s", job_id)
    return True































































































def public_url_to_path(url):

    ad_material_base = AD_MATERIAL_PUBLIC_BASE_URL.rstrip("/") + "/"

    if url and str(url).startswith(ad_material_base):

        rel_path = str(url)[len(ad_material_base):].lstrip("/").replace("/", os.sep)

        return os.path.join(AD_MATERIAL_PUBLIC_ROOT, rel_path)





























    base = PUBLIC_BASE_URL.rstrip("/") + "/"































    if not url or not str(url).startswith(base):































        return ""































    rel_path = str(url)[len(base):].lstrip("/").replace("/", os.sep)





























    return os.path.join(PUBLIC_ROOT, rel_path)





def cos_enabled():

    return bool(

        COS_SECRET_ID

        and COS_SECRET_KEY

        and COS_BUCKET

        and COS_REGION

        and COS_DOMAIN

        and CosConfig is not None

        and CosS3Client is not None

    )





def build_cos_object_key(path):

    normalized_path = os.path.abspath(path)

    ad_material_root = os.path.abspath(AD_MATERIAL_PUBLIC_ROOT)

    screenshot_root = os.path.abspath(SCREENSHOT_PUBLIC_ROOT)

    default_root = os.path.abspath(PUBLIC_ROOT)

    if normalized_path.startswith(ad_material_root + os.sep) or normalized_path == ad_material_root:

        rel_path = os.path.relpath(normalized_path, ad_material_root).replace(os.sep, "/").lstrip("/")

        return "ad-materials/" + rel_path

    if normalized_path.startswith(screenshot_root + os.sep) or normalized_path == screenshot_root:

        rel_path = os.path.relpath(normalized_path, screenshot_root).replace(os.sep, "/").lstrip("/")

        return "drama-screenshot-materials/" + rel_path

    rel_path = os.path.relpath(normalized_path, default_root).replace(os.sep, "/").lstrip("/")

    if COS_PREFIX:

        return COS_PREFIX + "/" + rel_path

    return rel_path





def build_cos_url(object_key):

    return "https://%s/%s" % (COS_DOMAIN.strip().strip("/"), object_key.lstrip("/"))





def guess_content_type(path):

    lower = str(path or "").lower()

    if lower.endswith(".jpg") or lower.endswith(".jpeg"):

        return "image/jpeg"

    if lower.endswith(".png"):

        return "image/png"

    if lower.endswith(".mp4"):

        return "video/mp4"

    if lower.endswith(".svg"):

        return "image/svg+xml"

    return "application/octet-stream"





def get_cos_client(timeout=None):

    if not cos_enabled():

        return None

    timeout = int(timeout or COS_UPLOAD_TIMEOUT)
    config = CosConfig(
        Region=COS_REGION,
        SecretId=COS_SECRET_ID,
        SecretKey=COS_SECRET_KEY,
        Timeout=timeout,
        KeepAlive=False,
    )

    return CosS3Client(config)





def upload_file_to_cos(path):

    if not cos_enabled():

        return build_public_url(path)

    if not file_ready(path):

        raise ValueError("cos upload source missing: %s" % path)

    object_key = build_cos_object_key(path)
    object_url = build_cos_url(object_key)
    expected_size = os.path.getsize(path)
    try:
        response = requests.head(object_url, timeout=(5, 15))
        if response.status_code == 200:
            remote_size = int(response.headers.get("Content-Length") or "-1")
            if remote_size == expected_size:
                logging.info("reuse existing COS object: %s", object_url)
                return object_url
    except Exception as exc:
        logging.warning("COS existing-object check failed, will upload: %s %s", object_url, exc)

    if expected_size >= COS_MULTIPART_THRESHOLD:
        logging.info("uploading large COS object with multipart: %s size=%s", object_url, expected_size)
        client = get_cos_client(timeout=max(COS_UPLOAD_TIMEOUT, COS_MULTIPART_TIMEOUT))
        client.upload_file(
            Bucket=COS_BUCKET,
            Key=object_key,
            LocalFilePath=path,
            PartSize=max(1, COS_MULTIPART_PART_SIZE_MB),
            MAXThread=max(1, COS_MULTIPART_THREADS),
            EnableMD5=False,
            ACL="public-read",
            ContentType=guess_content_type(path),
        )
    else:
        client = get_cos_client()

        with open(path, "rb") as fp:

            client.put_object(

                Bucket=COS_BUCKET,

                Body=fp,

                Key=object_key,

                EnableMD5=True,

                ACL="public-read",

                ContentType=guess_content_type(path),

            )

    return object_url





def publish_asset(path):

    if not file_ready(path):

        raise ValueError("publish asset source missing: %s" % path)

    if cos_enabled():

        return upload_file_to_cos(path)

    return build_public_url(path)





























































































def clamp_progress(value):































    try:































        value = int(round(float(value)))































    except Exception:































        value = 0































    return max(0, min(100, value))































































































def progress_for_status(status):































    return STATUS_PROGRESS.get(status, 0)































































































def status_label(status):































    return STATUS_LABELS.get(status, status)































































































def default_progress_detail(status):































    mapping = {































        "queued": "任务已进入队列",































        "validating": "校验剧集与资源可用性",































        "downloading": "正在下载素材",































        "processing_cover": "正在生成 16:9 封面",































        "rendering": "正在拼接合集视频",































        "removing_bgm": "正在去除 BGM",































        "done": "全部产物已生成",































        "failed": "任务执行失败",































    }































    return mapping.get(status, "")































































































def row_to_job(row):































    outputs = normalize_outputs(parse_json_text(row[14], {}))































    advanced = normalize_advanced_options(parse_json_text(row[15], {}))































    stored_progress = clamp_progress(row[17])































    if stored_progress <= 0 and row[16] != "failed":































        stored_progress = progress_for_status(row[16])































    progress_detail = (row[18] or "").strip() or default_progress_detail(row[16])































    return {































        "job_id": row[0],































        "app_id": row[1],































        "content_id": row[2],































        "app": product_name_for_app_id(row[1], row[3]),































        "country": row[4],































        "language": row[5],































        "drama_name": row[6],































        "episode_start": int(row[7]),































        "episode_end": int(row[8]),































        "total_episodes": int(row[9]),































        "cover_source_url": row[10],































        "cover_16x9_url": row[11],































        "output_video_url": row[12],































        "output_video_no_bgm_url": row[13],































        "outputs": outputs,































        "advanced_options": advanced,































        "status": row[16],































        "status_label": status_label(row[16]),































        "progress": stored_progress,































        "progress_detail": progress_detail,































        "error_message": row[19],















        "created_at": row[20],















        "updated_at": row[21],

        "finished_at": row[22] if len(row) > 22 else "",















        "creator_user_id": row[23] if len(row) > 23 else "",















        "creator_open_id": row[24] if len(row) > 24 else "",















        "creator_name": row[25] if len(row) > 25 else "",















        "completion_notified_at": row[26] if len(row) > 26 else "",















        "completion_notification_error": row[27] if len(row) > 27 else "",















        "result_preview": {















            "concat_video": row[12],















            "no_bgm_video": row[13],















            "cover_16x9": row[11],















        },















    }































































































def set_job_progress(job, status=None, progress=None, detail=None, persist=True):
    if job.get("_gpu_worker"):
        persist = False































    if status is not None:































        job["status"] = status































    if progress is None:































        progress = job.get("progress", progress_for_status(job.get("status", "queued")))































    job["progress"] = clamp_progress(progress)































    if detail is not None:































        job["progress_detail"] = str(detail or "").strip()































    elif "progress_detail" not in job:































        job["progress_detail"] = ""































    if persist:































        upsert_job_record(job)































    return job































































































def upsert_job_record(job):































    if is_job_deleted(job["job_id"]):































        return































    outputs_json = json.dumps(normalize_outputs(job.get("outputs", {})), ensure_ascii=False)































    advanced_json = json.dumps(































        normalize_advanced_options(job.get("advanced_options", {})), ensure_ascii=False































    )































    status_text = str(job.get("status", "queued") or "queued")

    progress = clamp_progress(job.get("progress", progress_for_status(status_text)))































    progress_detail = str(job.get("progress_detail", "") or "").strip()

    finished_at = str(job.get("finished_at", "") or "").strip()
    if status_text == "done":
        if not finished_at:
            finished_at = utc_now_text()
        job["finished_at"] = finished_at
    else:
        finished_at = ""
        job["finished_at"] = ""































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            conn.execute(































                """































                INSERT INTO drama_material_job (















                  job_id, app_id, content_id, app, country, language, drama_name,















                  episode_start, episode_end, total_episodes,















                  cover_source_url, cover_16x9_url, output_video_url,















                  output_video_no_bgm_url, outputs_json, advanced_options_json,















                  status, progress, progress_detail, error_message, created_at, updated_at, finished_at,















                  creator_user_id, creator_open_id, creator_name,















                  completion_notified_at, completion_notification_error















                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?)















                ON CONFLICT(job_id) DO UPDATE SET















                  app_id=excluded.app_id,















                  content_id=excluded.content_id,















                  app=excluded.app,















                  country=excluded.country,































                  language=excluded.language,































                  drama_name=excluded.drama_name,































                  episode_start=excluded.episode_start,































                  episode_end=excluded.episode_end,































                  total_episodes=excluded.total_episodes,































                  cover_source_url=excluded.cover_source_url,































                  cover_16x9_url=excluded.cover_16x9_url,































                  output_video_url=excluded.output_video_url,































                  output_video_no_bgm_url=excluded.output_video_no_bgm_url,































                  outputs_json=excluded.outputs_json,































                  advanced_options_json=excluded.advanced_options_json,































                  status=excluded.status,















                  progress=excluded.progress,















                  progress_detail=excluded.progress_detail,















                  error_message=excluded.error_message,















                  creator_user_id=CASE















                    WHEN TRIM(drama_material_job.creator_user_id) = '' THEN excluded.creator_user_id















                    ELSE drama_material_job.creator_user_id















                  END,















                  creator_open_id=CASE















                    WHEN TRIM(drama_material_job.creator_open_id) = '' THEN excluded.creator_open_id















                    ELSE drama_material_job.creator_open_id















                  END,















                  creator_name=CASE















                    WHEN TRIM(drama_material_job.creator_name) = '' THEN excluded.creator_name















                    ELSE drama_material_job.creator_name















                  END,















                  finished_at=CASE
                    WHEN excluded.status = 'done' AND TRIM(drama_material_job.finished_at) != '' THEN drama_material_job.finished_at
                    WHEN excluded.status = 'done' THEN excluded.finished_at
                    ELSE ''
                  END,

                  completion_notified_at=excluded.completion_notified_at,















                  completion_notification_error=excluded.completion_notification_error,















                  updated_at=CURRENT_TIMESTAMP















                """,















                (















                    job["job_id"],































                    str(job.get("app_id", "")),































                    job["content_id"],































                    job.get("app", ""),































                    job.get("country", ""),































                    job.get("language", ""),































                    job.get("drama_name", ""),































                    int(job.get("episode_start", 0)),































                    int(job.get("episode_end", 0)),































                    int(job.get("total_episodes", 0)),































                    job.get("cover_source_url", ""),































                    job.get("cover_16x9_url", ""),































                    job.get("output_video_url", ""),































                    job.get("output_video_no_bgm_url", ""),































                    outputs_json,































                    advanced_json,































                    status_text,































                    progress,















                    progress_detail,















                    job.get("error_message", ""),















                    finished_at,

                    str(job.get("creator_user_id", "") or ""),















                    str(job.get("creator_open_id", "") or ""),















                    str(job.get("creator_name", "") or ""),















                    str(job.get("completion_notified_at", "") or ""),















                    str(job.get("completion_notification_error", "") or ""),















                ),















            )















            conn.commit()































        finally:































            conn.close()































































































def fetch_job_row(job_id):































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            row = conn.execute(































                """































                SELECT































                  job_id, app_id, content_id, app, country, language, drama_name,































                  episode_start, episode_end, total_episodes,































                  cover_source_url, cover_16x9_url, output_video_url,















                  output_video_no_bgm_url, outputs_json, advanced_options_json,















                  status, progress, progress_detail, error_message, created_at, updated_at, finished_at,















                  creator_user_id, creator_open_id, creator_name,















                  completion_notified_at, completion_notification_error















                FROM drama_material_job















                WHERE job_id = ?















                """,















                (job_id,),































            ).fetchone()































        finally:































            conn.close()































    return enrich_material_job_timing(row_to_job(row)) if row else None































































































def find_duplicate_job(app_id, content_id, exclude_job_id=None):































    rows = fetch_job_rows(app_id=str(app_id or ""), content_id=str(content_id or ""), page=1, page_size=200)["items"]































    jobs = [row for row in rows if not exclude_job_id or row["job_id"] != exclude_job_id]































    if not jobs:































        return None































    jobs.sort(































        key=lambda item: (































            status_rank(item.get("status")),































            clamp_progress(item.get("progress", 0)),































            item.get("updated_at", ""),































            item.get("created_at", ""),































        ),































        reverse=True,































    )































    return jobs[0]































































































def ensure_no_duplicate_job(app_id, content_id, exclude_job_id=None):































    existing = find_duplicate_job(app_id, content_id, exclude_job_id=exclude_job_id)































    if not existing:































        return None































    if existing.get("status") == "failed":































        raise ValueError(































            "相同剧 ID 和产品已存在失败任务，请直接点击重新制作继续执行。job_id=%s" % existing["job_id"]































        )































    raise ValueError(































        "相同剧 ID 和产品已存在任务，禁止重复创建。job_id=%s status=%s"































        % (existing["job_id"], existing["status"])































    )































































































def fetch_job_rows(job_id=None, app_id=None, content_id=None, status=None, query=None, date_from=None, date_to=None, page=1, page_size=20):































    sql = """































    SELECT































      job_id, app_id, content_id, app, country, language, drama_name,































      episode_start, episode_end, total_episodes,































      cover_source_url, cover_16x9_url, output_video_url,















      output_video_no_bgm_url, outputs_json, advanced_options_json,















      status, progress, progress_detail, error_message, created_at, updated_at, finished_at,















      creator_user_id, creator_open_id, creator_name,















      completion_notified_at, completion_notification_error















    FROM drama_material_job















    """















    count_sql = "SELECT COUNT(*) FROM drama_material_job"































    filters = []































    params = []































    if job_id:































        filters.append("job_id = ?")































        params.append(job_id)































    if app_id:































        filters.append("app_id = ?")































        params.append(str(app_id))































    if content_id:































        filters.append("content_id = ?")































        params.append(content_id)































    if status and status != "all":































        if status == "processing":































            filters.append("status NOT IN ('done', 'failed')")































        else:































            filters.append("status = ?")































            params.append(status)































    if query:































        filters.append("(job_id LIKE ? OR content_id LIKE ? OR app LIKE ? OR drama_name LIKE ?)")































        fuzzy = "%{}%".format(query)































        params.extend([fuzzy, fuzzy, fuzzy, fuzzy])































    if date_from:































        filters.append("created_at >= ?")































        params.append(date_from + " 00:00:00")































    if date_to:































        filters.append("created_at <= ?")































        params.append(date_to + " 23:59:59")































    if filters:































        where = " WHERE " + " AND ".join(filters)































        sql += where































        count_sql += where































    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"































    page = max(1, int(page))































    page_size = max(1, min(100, int(page_size)))































    data_params = list(params) + [page_size, (page - 1) * page_size]































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            total = conn.execute(count_sql, params).fetchone()[0]































            rows = conn.execute(sql, data_params).fetchall()































        finally:































            conn.close()































    return {































        "items": [enrich_material_job_timing(row_to_job(row)) for row in rows],































        "count": len(rows),































        "total": int(total),































        "page": page,































        "page_size": page_size,































    }































































































def set_screenshot_job_progress(job, status=None, progress=None, detail=None, persist=True):

    if status is not None:

        job["status"] = status

    if progress is None:

        progress = job.get("progress", progress_for_status(job.get("status", "queued")))

    job["progress"] = clamp_progress(progress)

    if detail is not None:

        job["progress_detail"] = str(detail or "").strip()

    elif "progress_detail" not in job:

        job["progress_detail"] = ""

    if persist:

        upsert_screenshot_job_record(job)

    return job





def upsert_screenshot_job_record(job):

    if is_screenshot_job_deleted(job.get("job_id", "")):

        return

    assets_json = json.dumps(job.get("assets", {}), ensure_ascii=False)

    token_usage_json = json.dumps(job.get("token_usage", {}), ensure_ascii=False)

    elapsed_seconds = nonnegative_int(job.get("elapsed_seconds"), 0)

    token_total = nonnegative_int(job.get("token_total"), 0)

    progress = clamp_progress(job.get("progress", progress_for_status(job.get("status", "queued"))))

    progress_detail = str(job.get("progress_detail", "") or "").strip()

    with JOB_DB_LOCK:

        conn = get_job_db_connection()

        try:

            conn.execute(

                """

                INSERT INTO drama_screenshot_job (

                  job_id, app_id, content_id, app, country, language, drama_name,

                  cover_source_url, square_1x1_url, landscape_1_91x1_url, portrait_4x5_url,

                  assets_json, status, progress, progress_detail, error_message, created_at, updated_at,

                  started_at, finished_at, elapsed_seconds, token_total, token_usage_json,

                  creator_user_id, creator_open_id, creator_name

                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?)

                ON CONFLICT(job_id) DO UPDATE SET

                  app_id=excluded.app_id,

                  content_id=excluded.content_id,

                  app=excluded.app,

                  country=excluded.country,

                  language=excluded.language,

                  drama_name=excluded.drama_name,

                  cover_source_url=excluded.cover_source_url,

                  square_1x1_url=excluded.square_1x1_url,

                  landscape_1_91x1_url=excluded.landscape_1_91x1_url,

                  portrait_4x5_url=excluded.portrait_4x5_url,

                  assets_json=excluded.assets_json,

                  status=excluded.status,

                  progress=excluded.progress,

                  progress_detail=excluded.progress_detail,

                  error_message=excluded.error_message,

                  started_at=excluded.started_at,

                  finished_at=excluded.finished_at,

                  elapsed_seconds=excluded.elapsed_seconds,

                  token_total=excluded.token_total,

                  token_usage_json=excluded.token_usage_json,

                  creator_user_id=CASE

                    WHEN TRIM(drama_screenshot_job.creator_user_id) = '' THEN excluded.creator_user_id

                    ELSE drama_screenshot_job.creator_user_id

                  END,

                  creator_open_id=CASE

                    WHEN TRIM(drama_screenshot_job.creator_open_id) = '' THEN excluded.creator_open_id

                    ELSE drama_screenshot_job.creator_open_id

                  END,

                  creator_name=CASE

                    WHEN TRIM(drama_screenshot_job.creator_name) = '' THEN excluded.creator_name

                    ELSE drama_screenshot_job.creator_name

                  END,

                  updated_at=CURRENT_TIMESTAMP

                """,

                (

                    job["job_id"],

                    str(job.get("app_id", "")),

                    job["content_id"],

                    job.get("app", ""),

                    job.get("country", ""),

                    job.get("language", ""),

                    job.get("drama_name", ""),

                    job.get("cover_source_url", ""),

                    job.get("square_1x1_url", ""),

                    job.get("landscape_1_91x1_url", ""),

                    job.get("portrait_4x5_url", ""),

                    assets_json,

                    job.get("status", "queued"),

                    progress,

                    progress_detail,

                    job.get("error_message", ""),

                    job.get("started_at", ""),

                    job.get("finished_at", ""),

                    elapsed_seconds,

                    token_total,

                    token_usage_json,

                    job.get("creator_user_id", ""),

                    job.get("creator_open_id", ""),

                    job.get("creator_name", ""),

                ),

            )

            conn.commit()

        finally:

            conn.close()





TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def normalize_token_usage_payload(value):
    if not isinstance(value, dict):
        return {}
    usage = {}
    for field in TOKEN_USAGE_FIELDS:
        usage[field] = nonnegative_int(value.get(field), 0)
    if not usage.get("total_tokens"):
        usage["total_tokens"] = nonnegative_int(value.get("token_total") or value.get("total"), 0)
    if not any(usage.values()):
        return {}
    if value.get("session_count") is not None:
        usage["session_count"] = nonnegative_int(value.get("session_count"), 0)
    return usage


def record_screenshot_token_usage(job, usage, source="codex_screenshot"):
    usage = normalize_token_usage_payload(usage)
    if not usage:
        return
    current = job.get("token_usage", {})
    if not isinstance(current, dict):
        current = {}
    runs = current.get("runs")
    if not isinstance(runs, list):
        runs = []
    entry = dict(usage)
    entry["source"] = source
    entry["recorded_at"] = utc_now_text()
    runs.append(entry)
    merged = {"runs": runs}
    for field in TOKEN_USAGE_FIELDS:
        merged[field] = sum(nonnegative_int(item.get(field), 0) for item in runs if isinstance(item, dict))
    job["token_total"] = merged["total_tokens"]
    job["token_usage"] = merged
    upsert_screenshot_job_record(job)


def begin_screenshot_job_run(job):
    if not str(job.get("started_at", "") or "").strip():
        job["started_at"] = utc_now_text()
    job["finished_at"] = ""
    job["elapsed_seconds"] = 0
    upsert_screenshot_job_record(job)


def finish_screenshot_job_run(job):
    finished_at = utc_now_text()
    started_at = str(job.get("started_at", "") or "").strip() or finished_at
    job["started_at"] = started_at
    job["finished_at"] = finished_at
    start_dt = parse_job_timestamp(started_at)
    end_dt = parse_job_timestamp(finished_at)
    if start_dt and end_dt and end_dt >= start_dt:
        job["elapsed_seconds"] = int((end_dt - start_dt).total_seconds())
    else:
        job["elapsed_seconds"] = 0


def app_package_for_app_id(app_id):
    mapping = {
        "1479": "com.dramawave.app",
        "979": "com.freereels.app",
    }
    app_id = str(app_id or "").strip()
    return mapping.get(app_id, app_id)


def mysql_escape_literal(value):
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def lookup_admin_username_by_email(email):
    email = str(email or "").strip()
    if not email:
        return ""
    if not (ADMIN_MAPPING_MYSQL_HOST and ADMIN_MAPPING_MYSQL_USER and ADMIN_MAPPING_MYSQL_DATABASE):
        return ""
    query = (
        "SELECT au.username "
        "FROM admin_user_group aug "
        "JOIN admin_users au ON au.id = aug.sub_user_id "
        "WHERE LOWER(TRIM(aug.email)) = LOWER(TRIM('%s')) "
        "LIMIT 1"
    ) % mysql_escape_literal(email)
    env = os.environ.copy()
    env["MYSQL_PWD"] = ADMIN_MAPPING_MYSQL_PASSWORD
    cmd = [
        "mysql",
        "-h", ADMIN_MAPPING_MYSQL_HOST,
        "-P", ADMIN_MAPPING_MYSQL_PORT,
        "-u", ADMIN_MAPPING_MYSQL_USER,
        "--default-character-set=utf8mb4",
        "--connect-timeout=%s" % max(1, min(ADMIN_MAPPING_MYSQL_TIMEOUT, 30)),
        "-N",
        "-B",
        ADMIN_MAPPING_MYSQL_DATABASE,
        "-e",
        query,
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=ADMIN_MAPPING_MYSQL_TIMEOUT,
            env=env,
            universal_newlines=True,
        )
    except Exception:
        logging.exception("failed to lookup admin username by email")
        return ""
    if result.returncode != 0:
        logging.warning("admin username mysql lookup failed: %s", (result.stderr or "").strip())
        return ""
    return (result.stdout or "").strip().splitlines()[0].strip() if (result.stdout or "").strip() else ""


def get_admin_user_callback_username(user_id):
    user_id = str(user_id or "").strip()
    if not user_id:
        return ""
    data = {"user_id": user_id}
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            user_columns = {row[1] for row in conn.execute("PRAGMA table_info(drama_admin_user)").fetchall()}
            select_fields = ["user_id", "name"]
            if "username" in user_columns:
                select_fields.append("username")
            if "email" in user_columns:
                select_fields.append("email")
            if "en_name" in user_columns:
                select_fields.append("en_name")
            row = conn.execute(
                "SELECT %s FROM drama_admin_user WHERE user_id = ?" % ", ".join(select_fields),
                (user_id,),
            ).fetchone()
            if not row:
                return user_id
            data = dict(zip(select_fields, row))
        finally:
            conn.close()
    feishu_email = str(data.get("email", "") or "").strip()
    mapped_username = lookup_admin_username_by_email(feishu_email)
    if mapped_username:
        return mapped_username
    for key in ("username", "email", "name", "en_name", "user_id"):
        value = str(data.get(key, "") or "").strip()
        if value:
            return value
    return user_id

def callback_time_text(value):
    value = str(value or "").strip()
    if not value:
        return now_text()
    try:
        dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S") + timedelta(hours=8)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return value[:19]


def screenshot_material_id(job_id, key):
    import hashlib
    digest = hashlib.sha1((str(job_id) + ":" + str(key)).encode("utf-8")).hexdigest()
    return int(digest[:15], 16)


def screenshot_asset_name(job, spec):
    return "%s-%s-%s-%s" % (
        str(job.get("content_id", "") or ""),
        str(job.get("drama_name", "") or "screenshot"),
        str(spec.get("ratio", "") or spec.get("label", "")),
        str(job.get("job_id", "") or "")[:8],
    )


def post_screenshot_ai_source_callback(job):
    if not AI_SOURCE_CALLBACK_ENABLED:
        return {"skipped": True, "reason": "disabled"}
    if not AI_SOURCE_CALLBACK_URL or not AI_SOURCE_CALLBACK_TOKEN:
        return {"skipped": True, "reason": "not_configured"}
    creator = get_admin_user_callback_username(job.get("creator_user_id", "")) or str(job.get("creator_name", "") or "").strip()
    created_at = callback_time_text(job.get("updated_at") or job.get("created_at") or "")
    items = []
    for spec in SCREENSHOT_SPECS:
        url = str(job.get(spec["field"], "") or "").strip()
        if not url:
            continue
        items.append({
            "app_package": app_package_for_app_id(job.get("app_id", "")),
            "material_id": screenshot_material_id(job.get("job_id", ""), spec["key"]),
            "content_id": str(job.get("content_id", "") or ""),
            "category": spec["ratio"],
            "name": screenshot_asset_name(job, spec),
            "url": url,
            "email": creator,
            "created_at": created_at,
        })
    if not items:
        return {"skipped": True, "reason": "no_assets"}
    headers = {
        "Authorization": "Bearer %s" % AI_SOURCE_CALLBACK_TOKEN,
        "Authrization": "Bearer %s" % AI_SOURCE_CALLBACK_TOKEN,
        "Content-Type": "application/json",
    }
    response = requests.post(
        AI_SOURCE_CALLBACK_URL,
        headers=headers,
        json=items[:50],
        timeout=AI_SOURCE_CALLBACK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError("ai source callback failed (%s): %s" % (response.status_code, response.text[:500]))
    return {"sent_count": len(items), "status_code": response.status_code, "response": response.text[:500]}


def notify_screenshot_ai_source_callback(job, raise_on_error=False):
    try:
        result = post_screenshot_ai_source_callback(job)
        logging.info("screenshot ai source callback result: %s %s", job.get("job_id", ""), result)
        append_audit_log(
            None,
            "callback_screenshot_ai_source",
            "screenshot_job",
            job.get("job_id", ""),
            result,
        )
        return result
    except Exception as exc:
        logging.exception("screenshot ai source callback failed: %s", job.get("job_id", ""))
        try:
            append_audit_log(
                None,
                "callback_screenshot_ai_source_failed",
                "screenshot_job",
                job.get("job_id", ""),
                {"error": str(exc)},
            )
        except Exception:
            logging.exception("failed to write callback failure audit log")
        if raise_on_error:
            raise
        return {"failed": True, "error": str(exc)}


def fetch_screenshot_job_row(job_id):

    with JOB_DB_LOCK:

        conn = get_job_db_connection()

        try:

            row = conn.execute(

                """

                SELECT

                  job_id, app_id, content_id, app, country, language, drama_name,

                  cover_source_url, square_1x1_url, landscape_1_91x1_url, portrait_4x5_url,

                  assets_json, status, progress, progress_detail, error_message, created_at, updated_at,

                  started_at, finished_at, elapsed_seconds, token_total, token_usage_json,

                  creator_user_id, creator_open_id, creator_name

                FROM drama_screenshot_job

                WHERE job_id = ?

                """,

                (job_id,),

            ).fetchone()

        finally:

            conn.close()

    return row_to_screenshot_job(row) if row else None





def fetch_screenshot_job_rows(job_id=None, app_id=None, content_id=None, status=None, query=None, date_from=None, date_to=None, page=1, page_size=20):

    sql = """

    SELECT

      job_id, app_id, content_id, app, country, language, drama_name,

      cover_source_url, square_1x1_url, landscape_1_91x1_url, portrait_4x5_url,

      assets_json, status, progress, progress_detail, error_message, created_at, updated_at,

      started_at, finished_at, elapsed_seconds, token_total, token_usage_json,

      creator_user_id, creator_open_id, creator_name

    FROM drama_screenshot_job

    """

    count_sql = "SELECT COUNT(*) FROM drama_screenshot_job"

    filters = []

    params = []

    if job_id:

        filters.append("job_id = ?")

        params.append(job_id)

    if app_id:

        filters.append("app_id = ?")

        params.append(str(app_id))

    if content_id:

        filters.append("content_id = ?")

        params.append(content_id)

    if status and status != "all":

        if status == "processing":

            filters.append("status NOT IN ('done', 'failed')")

        else:

            filters.append("status = ?")

            params.append(status)

    if query:

        filters.append("(job_id LIKE ? OR content_id LIKE ? OR app LIKE ? OR drama_name LIKE ?)")

        fuzzy = "%{}%".format(query)

        params.extend([fuzzy, fuzzy, fuzzy, fuzzy])

    if date_from:

        filters.append("created_at >= ?")

        params.append(date_from + " 00:00:00")

    if date_to:

        filters.append("created_at <= ?")

        params.append(date_to + " 23:59:59")

    if filters:

        where = " WHERE " + " AND ".join(filters)

        sql += where

        count_sql += where

    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"

    page = max(1, int(page))

    page_size = max(1, min(100, int(page_size)))

    data_params = list(params) + [page_size, (page - 1) * page_size]

    with JOB_DB_LOCK:

        conn = get_job_db_connection()

        try:

            total = conn.execute(count_sql, params).fetchone()[0]

            rows = conn.execute(sql, data_params).fetchall()

        finally:

            conn.close()

    return {

        "items": [row_to_screenshot_job(row) for row in rows],

        "count": len(rows),

        "total": int(total),

        "page": page,

        "page_size": page_size,

    }







def delete_screenshot_jobs(job_ids):

    normalized = []

    seen = set()

    for item in job_ids or []:

        job_id = str(item or "").strip()

        if re.fullmatch(r"[0-9a-f]{32}", job_id) and job_id not in seen:

            normalized.append(job_id)

            seen.add(job_id)

    if not normalized:

        return {"requested_count": 0, "deleted_count": 0, "missing_count": 0, "deleted_ids": [], "missing_ids": []}

    existing = set()

    with JOB_DB_LOCK:

        conn = get_job_db_connection()

        try:

            placeholders = ",".join(["?"] * len(normalized))

            rows = conn.execute("SELECT job_id FROM drama_screenshot_job WHERE job_id IN (%s)" % placeholders, normalized).fetchall()

            existing = {row[0] for row in rows}

            if existing:

                conn.execute("DELETE FROM drama_screenshot_job WHERE job_id IN (%s)" % ",".join(["?"] * len(existing)), list(existing))

                conn.commit()

        finally:

            conn.close()

    for job_id in existing:

        mark_screenshot_job_deleted(job_id)

        shutil.rmtree(os.path.join(SCREENSHOT_WORK_ROOT, job_id), ignore_errors=True)

        shutil.rmtree(os.path.join(SCREENSHOT_PUBLIC_ROOT, job_id), ignore_errors=True)

    missing = [job_id for job_id in normalized if job_id not in existing]

    return {

        "requested_count": len(normalized),

        "deleted_count": len(existing),

        "missing_count": len(missing),

        "deleted_ids": sorted(existing),

        "missing_ids": missing,

    }


def delete_screenshot_job(job_id):

    result = delete_screenshot_jobs([job_id])

    return result if result.get("deleted_count") else None
def find_duplicate_screenshot_job(app_id, content_id):

    rows = fetch_screenshot_job_rows(

        app_id=str(app_id or ""),

        content_id=str(content_id or ""),

        page=1,

        page_size=200,

    )["items"]

    if not rows:

        return None

    rows.sort(

        key=lambda item: (

            status_rank(item.get("status")),

            clamp_progress(item.get("progress", 0)),

            item.get("updated_at", ""),

            item.get("created_at", ""),

        ),

        reverse=True,

    )

    return rows[0]





def ensure_no_duplicate_screenshot_job(app_id, content_id):

    existing = find_duplicate_screenshot_job(app_id, content_id)

    if not existing:

        return None

    raise StructuredApiError(
        "duplicate_screenshot_job",
        "相同产品下该剧已存在截图素材制作记录，禁止重复创建。",
        job_id=existing.get("job_id", ""),
        status=existing.get("status", ""),
        app_id=str(app_id or ""),
        content_id=str(content_id or ""),
    )





def submit_screenshot_job(payload, actor_session=None):

    app_id = str(payload.get("app_id", "")).strip()

    content_id = str(payload.get("content_id", "")).strip()

    if not app_id:

        raise ValueError("app_id 不能为空")

    if not content_id:

        raise ValueError("content_id 不能为空")

    ensure_no_duplicate_screenshot_job(app_id, content_id)

    validation = validate_screenshot_request(app_id, content_id)

    job = {

        "job_id": uuid.uuid4().hex,

        "app_id": validation["app_id"],

        "content_id": content_id,

        "app": validation["app"],

        "country": validation["country"],

        "language": validation["language"],

        "drama_name": validation["drama_name"],

        "cover_source_url": validation["cover_source_url"],

        "square_1x1_url": "",

        "landscape_1_91x1_url": "",

        "portrait_4x5_url": "",

        "assets": {},

        "status": "queued",

        "progress": 2,

        "progress_detail": "截图素材任务已进入队列",

        "error_message": "",

        "creator_user_id": (actor_session or {}).get("user_id", ""),

        "creator_open_id": (actor_session or {}).get("open_id", ""),

        "creator_name": (actor_session or {}).get("name", ""),

    }

    clear_screenshot_job_deleted_marker(job["job_id"])
    upsert_screenshot_job_record(job)

    run_screenshot_job_async(job)

    return {

        "message": "job accepted",

        "job_id": job["job_id"],

        "status": job["status"],

        "app_id": validation["app_id"],

        "content_id": content_id,

        "drama_name": validation["drama_name"],

        "app": validation["app"],

        "country": validation["country"],

        "language": validation["language"],

        "cover_source_url": validation["cover_source_url"],

        "created_at": now_text(),

    }





def submit_screenshot_job_batch(payload, actor_session=None):

    app_id = str(payload.get("app_id", "") or "").strip()

    raw_content_ids = payload.get("content_ids")

    content_ids = []

    if isinstance(raw_content_ids, list):

        content_ids = [str(item or "").strip() for item in raw_content_ids]

    elif isinstance(raw_content_ids, str):

        content_ids = [line.strip() for line in raw_content_ids.replace(",", "\n").splitlines()]

    elif payload.get("content_ids_text") is not None:

        content_ids = [line.strip() for line in str(payload.get("content_ids_text", "")).replace(",", "\n").splitlines()]

    content_ids = [item for item in content_ids if item]

    if not app_id:

        raise ValueError("app_id 不能为空")

    if not content_ids:

        raise ValueError("content_ids 不能为空")



    seen = set()

    ordered_content_ids = []

    for content_id in content_ids:

        if content_id in seen:

            continue

        seen.add(content_id)

        ordered_content_ids.append(content_id)



    items = []

    accepted = 0

    duplicates = 0

    failed = 0

    for content_id in ordered_content_ids:

        try:

            result = submit_screenshot_job({"app_id": app_id, "content_id": content_id}, actor_session)

            result["accepted"] = True

            items.append(result)

            accepted += 1

        except Exception as exc:

            message = str(exc).strip() or exc.__class__.__name__

            existing = find_duplicate_screenshot_job(app_id, content_id)

            if existing:

                duplicates += 1

                items.append({

                    "accepted": False,

                    "duplicate": True,

                    "app_id": app_id,

                    "content_id": content_id,

                    "job_id": existing.get("job_id", ""),

                    "status": existing.get("status", ""),

                    "status_label": existing.get("status_label", ""),

                    "progress": existing.get("progress", 0),

                    "progress_detail": existing.get("progress_detail", ""),

                    "drama_name": existing.get("drama_name", ""),

                    "app": existing.get("app", product_name_for_app_id(app_id, "")),

                    "error": message,

                })

            else:

                failed += 1

                items.append({

                    "accepted": False,

                    "duplicate": False,

                    "app_id": app_id,

                    "content_id": content_id,

                    "error": message,

                })

    if accepted == 0 and failed > 0 and duplicates == 0:

        first_error = next((str(item.get("error", "")).strip() for item in items if item.get("error")), "")

        raise ValueError("全部截图任务创建失败：%s" % (first_error or "请检查 content_id 是否正确"))



    return {

        "message": "batch accepted",

        "app_id": app_id,

        "count": len(ordered_content_ids),

        "accepted_count": accepted,

        "duplicate_count": duplicates,

        "failed_count": failed,

        "items": items,

    }





def is_screenshot_source_consistency_rejection(exc):
    text = str(exc or "").lower()
    return (
        "source consistency rejected" in text
        or ("source_consistency" in text and "passed" in text and "false" in text)
    )


def is_screenshot_generation_no_output_error(exc):
    text = str(exc or "").lower()
    keywords = (
        "ai image generation produced no output",
        "image generation produced no output",
        "image generation tool returned usererror",
        "built-in ai image generation failed",
        "built-in image generation failed",
        "built-in ai image generation/editing failed",
        "no usable ai-generated image",
        "no raw ai-generated image",
        "no generated image was returned",
        "no image path was returned",
        "no output path is available",
        "missing raw_generated_path",
    )
    return any(keyword in text for keyword in keywords)


def is_screenshot_batch_recoverable_error(exc):
    if is_screenshot_source_consistency_rejection(exc):
        return True
    if is_screenshot_generation_no_output_error(exc):
        return False
    text = str(exc or "").lower()
    recoverable_keywords = (
        "raw aspect ratio rejected",
        "screenshot batch incomplete",
    )
    return any(keyword in text for keyword in recoverable_keywords)


def is_screenshot_batch_fallback_error(exc):
    return is_screenshot_batch_recoverable_error(exc) or is_screenshot_generation_no_output_error(exc)


def set_screenshot_batch_remake_progress(job, exc=None):
    if is_screenshot_source_consistency_rejection(exc):
        return set_screenshot_consistency_remake_progress(job)
    return set_screenshot_job_progress(
        job,
        status="processing_cover",
        progress=max(38, clamp_progress(job.get("progress", 38))),
        detail="??????????????????",
    )


def set_screenshot_consistency_remake_progress(job, spec=None, attempt=None, max_retries=None, persist=True):
    label = str((spec or {}).get("label", "") or "").strip()
    detail = "校验不通过重新制作中"
    if label:
        detail = "校验不通过重新制作中：正在重新制作 %s" % label
    if attempt is not None and max_retries is not None:
        detail = "%s（第 %d/%d 次）" % (detail, max(1, int(attempt)), max(1, int(max_retries)))
    return set_screenshot_job_progress(
        job,
        status="validation_remaking",
        progress=max(38, clamp_progress(job.get("progress", 38))),
        detail=detail,
        persist=persist,
    )


def cleanup_screenshot_output_paths(*paths, remove_ready=False):
    for partial_path in paths:
        try:
            if not partial_path or not os.path.exists(partial_path):
                continue
            if remove_ready or not file_ready(partial_path):
                os.remove(partial_path)
        except OSError:
            logging.warning("failed to remove partial screenshot output: %s", partial_path)


def process_screenshot_job(job):

    workdir = os.path.join(SCREENSHOT_WORK_ROOT, job["job_id"])

    source_dir = os.path.join(workdir, "source")

    generated_dir = os.path.join(workdir, "generated")

    public_dir = os.path.join(SCREENSHOT_PUBLIC_ROOT, job["job_id"])

    ensure_dir(source_dir)

    ensure_dir(generated_dir)

    ensure_dir(public_dir)



    set_screenshot_job_progress(job, status="validating", progress=6, detail="\u6821\u9a8c\u5c01\u9762\u7d20\u6750\u53ef\u7528\u6027")

    existing_cover_source_url = str(job.get("cover_source_url", "") or "").strip()
    if existing_cover_source_url:
        validation = {
            "app_id": job["app_id"],
            "app": job.get("app", ""),
            "country": job.get("country", ""),
            "language": job.get("language", ""),
            "drama_name": job.get("drama_name", ""),
            "cover_source_url": existing_cover_source_url,
        }
    else:
        validation = validate_screenshot_request(job["app_id"], job["content_id"])

    job["app_id"] = validation["app_id"]

    job["app"] = validation["app"]

    job["country"] = validation["country"]

    job["language"] = validation["language"]

    job["drama_name"] = validation["drama_name"]

    job["cover_source_url"] = validation["cover_source_url"]

    upsert_screenshot_job_record(job)



    source_path = os.path.join(source_dir, "cover_source.jpg")

    if file_ready(source_path) and not image_file_ready(source_path):
        logging.warning("cached screenshot source image is invalid; redownloading: %s", source_path)
        remove_file_quietly(source_path)

    if file_ready(source_path):

        set_screenshot_job_progress(job, status="downloading", progress=16, detail="\u590d\u7528\u5df2\u4e0b\u8f7d\u5c01\u9762\u7d20\u6750")

    else:

        set_screenshot_job_progress(job, status="downloading", progress=12, detail="\u5f00\u59cb\u4e0b\u8f7d\u5c01\u9762\u7d20\u6750")

        download_file(validation["cover_source_url"], source_path)

        if image_file_ready(source_path):
            set_screenshot_job_progress(job, status="downloading", progress=24, detail="\u5c01\u9762\u7d20\u6750\u4e0b\u8f7d\u5b8c\u6210")
        else:
            logging.warning(
                "downloaded screenshot source image is invalid; sidecar will retry source_url candidates: job=%s url=%s",
                job.get("job_id"),
                validation["cover_source_url"],
            )
            remove_file_quietly(source_path)
            set_screenshot_job_progress(job, status="downloading", progress=24, detail="封面素材下载异常，将由生成服务重新拉取")



    assets = job.get("assets", {})

    if not isinstance(assets, dict):

        assets = {}



    total_specs = float(len(SCREENSHOT_SPECS))
    pending = []

    for index, spec in enumerate(SCREENSHOT_SPECS, 1):
        workspace_output_path = os.path.join(generated_dir, spec["filename"])
        public_output_path = os.path.join(public_dir, spec["filename"])
        current_url = str(job.get(spec["field"], "") or "").strip()
        progress_after = 24 + int(index * 60 / total_specs)

        if file_ready(public_output_path):
            asset_url = current_url or publish_asset(public_output_path)
            job[spec["field"]] = asset_url
            assets[spec["key"]] = {
                "label": spec["label"],
                "ratio": spec["ratio"],
                "width": spec["width"],
                "height": spec["height"],
                "url": asset_url,
                "public_output_path": public_output_path,
                "workspace_output_path": workspace_output_path,
            }
            set_screenshot_job_progress(
                job,
                status="processing_cover",
                progress=max(38, progress_after),
                detail="\u590d\u7528\u5df2\u6709 %s" % spec["label"],
            )
            continue

        pending.append((index, spec, workspace_output_path, public_output_path))

    if pending:
        set_screenshot_job_progress(
            job,
            status="processing_cover",
            progress=38,
            detail="\u5e76\u884c\u751f\u6210 %d \u4e2a\u622a\u56fe\u5c3a\u5bf8" % len(pending),
        )

        def generate_one(item):
            index, spec, workspace_output_path, public_output_path = item
            generate_screenshot_via_codex_service(
                job,
                source_path,
                [
                    {
                        "key": spec["key"],
                        "label": spec["label"],
                        "ratio": spec["ratio"],
                        "width": spec["width"],
                        "height": spec["height"],
                        "workspace_output_path": workspace_output_path,
                        "public_output_path": public_output_path,
                        "public_base_url": SCREENSHOT_PUBLIC_BASE_URL,
                    }
                ],
            )
            if not file_ready(public_output_path):
                raise RuntimeError("\u7f3a\u5c11\u751f\u6210\u7ed3\u679c: %s" % spec["filename"])
            asset_url = publish_asset(public_output_path)
            return index, spec, workspace_output_path, public_output_path, asset_url

        generate_one_once = generate_one

        if CODEX_SCREENSHOT_BATCH_ENABLED and len(pending) > 1:
            batch_fallback_to_single = False
            batch_items = []
            for _, spec, workspace_output_path, public_output_path in pending:
                batch_items.append(
                    {
                        "key": spec["key"],
                        "label": spec["label"],
                        "ratio": spec["ratio"],
                        "width": spec["width"],
                        "height": spec["height"],
                        "workspace_output_path": workspace_output_path,
                        "public_output_path": public_output_path,
                        "public_base_url": SCREENSHOT_PUBLIC_BASE_URL,
                    }
                )
            try:
                generate_screenshot_via_codex_service_batch(job, source_path, batch_items)
            except Exception as exc:
                if is_screenshot_batch_fallback_error(exc):
                    logging.warning(
                        "screenshot batch failed; falling back to per-size generation: job=%s error=%s",
                        job["job_id"],
                        str(exc).strip() or exc.__class__.__name__,
                    )
                    batch_fallback_to_single = True
                    set_screenshot_batch_remake_progress(job, exc)
                elif CODEX_SCREENSHOT_BATCH_STRICT:
                    logging.exception("screenshot batch failed: %s", job["job_id"])
                    raise

            remaining = []
            completed = 0
            for item in pending:
                _, spec, workspace_output_path, public_output_path = item
                if not file_ready(public_output_path):
                    remaining.append(item)
                    continue
                asset_url = publish_asset(public_output_path)
                completed += 1
                job[spec["field"]] = asset_url
                assets[spec["key"]] = {
                    "label": spec["label"],
                    "ratio": spec["ratio"],
                    "width": spec["width"],
                    "height": spec["height"],
                    "url": asset_url,
                    "public_output_path": public_output_path,
                    "workspace_output_path": workspace_output_path,
                }
                job["assets"] = assets
                progress = 38 + int(46 * completed / max(1, len(pending)))
                set_screenshot_job_progress(
                    job,
                    status="processing_cover",
                    progress=progress,
                    detail="\u5df2\u6279\u91cf\u751f\u6210\u5e76\u4e0a\u4f20 %s" % spec["label"],
                )
            pending = remaining
            if CODEX_SCREENSHOT_BATCH_STRICT and pending:
                message = "screenshot batch incomplete: %s" % ",".join(
                    str(item[1].get("key", "")) for item in pending
                )
                if batch_fallback_to_single:
                    logging.warning("%s; continuing with per-size remake", message)
                else:
                    logging.warning("%s; continuing with per-size remake", message)
                    batch_fallback_to_single = True
                    set_screenshot_batch_remake_progress(job, RuntimeError(message))

        def generate_one(item):
            index, spec, workspace_output_path, public_output_path = item
            attempts = max(1, SCREENSHOT_ITEM_RETRY_ATTEMPTS + 1)
            last_error = None
            retrying_after_consistency_rejection = False
            for attempt in range(1, attempts + 1):
                try:
                    if attempt > 1:
                        if retrying_after_consistency_rejection:
                            set_screenshot_consistency_remake_progress(
                                job,
                                spec,
                                attempt - 1,
                                SCREENSHOT_ITEM_RETRY_ATTEMPTS,
                            )
                        else:
                            set_screenshot_job_progress(
                                job,
                                status="processing_cover",
                                progress=max(38, clamp_progress(job.get("progress", 38))),
                                detail="\u91cd\u8bd5\u751f\u6210 %s\uff08\u7b2c %d/%d \u6b21\uff09"
                                % (spec["label"], attempt - 1, SCREENSHOT_ITEM_RETRY_ATTEMPTS),
                            )
                    return generate_one_once(item)
                except Exception as exc:
                    last_error = exc
                    retrying_after_consistency_rejection = is_screenshot_source_consistency_rejection(exc)
                    if file_ready(public_output_path) and not retrying_after_consistency_rejection:
                        asset_url = publish_asset(public_output_path)
                        return index, spec, workspace_output_path, public_output_path, asset_url
                    if is_screenshot_generation_no_output_error(exc):
                        logging.warning(
                            "screenshot size hit hard image-generation no-output failure: job=%s key=%s error=%s",
                            job["job_id"],
                            spec["key"],
                            str(exc).strip() or exc.__class__.__name__,
                        )
                        break
                    if attempt >= attempts:
                        break
                    if retrying_after_consistency_rejection:
                        set_screenshot_consistency_remake_progress(
                            job,
                            spec,
                            attempt,
                            SCREENSHOT_ITEM_RETRY_ATTEMPTS,
                        )
                    logging.warning(
                        "screenshot size retrying: job=%s key=%s attempt=%s/%s error=%s",
                        job["job_id"],
                        spec["key"],
                        attempt,
                        attempts,
                        str(exc).strip() or exc.__class__.__name__,
                    )
                    cleanup_screenshot_output_paths(
                        workspace_output_path,
                        public_output_path,
                        remove_ready=retrying_after_consistency_rejection,
                    )
            raise last_error or RuntimeError("\u751f\u6210\u5931\u8d25: %s" % spec["filename"])

        completed = 0
        errors = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(pending))) as executor:
            future_map = {executor.submit(generate_one, item): item for item in pending}
            for future in concurrent.futures.as_completed(future_map):
                index, spec, workspace_output_path, public_output_path = future_map[future]
                try:
                    _, spec, workspace_output_path, public_output_path, asset_url = future.result()
                except Exception as exc:
                    logging.exception("screenshot size failed: %s %s", job["job_id"], spec["key"])
                    errors.append("%s: %s" % (spec["label"], str(exc).strip() or exc.__class__.__name__))
                    continue

                completed += 1
                job[spec["field"]] = asset_url
                assets[spec["key"]] = {
                    "label": spec["label"],
                    "ratio": spec["ratio"],
                    "width": spec["width"],
                    "height": spec["height"],
                    "url": asset_url,
                    "public_output_path": public_output_path,
                    "workspace_output_path": workspace_output_path,
                }
                job["assets"] = assets
                progress = 38 + int(46 * completed / max(1, len(pending)))
                set_screenshot_job_progress(
                    job,
                    status="processing_cover",
                    progress=progress,
                    detail="\u5df2\u751f\u6210\u5e76\u4e0a\u4f20 %s" % spec["label"],
                )

        if errors:
            job["assets"] = assets
            upsert_screenshot_job_record(job)
            raise RuntimeError("; ".join(errors))

    job["assets"] = assets

    job["error_message"] = ""

    set_screenshot_job_progress(
        job,
        status="processing_cover",
        progress=96,
        detail="\u4e09\u79cd\u622a\u56fe\u7d20\u6750\u5df2\u751f\u6210\uff0c\u6b63\u5728\u56de\u4f20\u7d20\u6750\u63a5\u53e3",
    )

    callback_job = fetch_screenshot_job_row(job["job_id"]) or job

    notify_screenshot_ai_source_callback(callback_job, raise_on_error=True)

    finish_screenshot_job_run(job)

    set_screenshot_job_progress(job, status="done", progress=100, detail="\u4e09\u79cd\u622a\u56fe\u7d20\u6750\u5df2\u5168\u90e8\u751f\u6210\u5e76\u56de\u4f20")





def screenshot_job_backlog_count():
    try:
        conn = get_job_db_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM drama_screenshot_job WHERE status = 'queued'"
            ).fetchone()
            return int(row["count"] if row else 0)
        finally:
            conn.close()
    except Exception:
        logging.exception("failed to count screenshot backlog")
        return 0


def screenshot_job_capacity():
    backlog = screenshot_job_backlog_count()
    if backlog > SCREENSHOT_JOB_BURST_QUEUE_THRESHOLD:
        return SCREENSHOT_JOB_BURST_CONCURRENCY
    return SCREENSHOT_JOB_BASE_CONCURRENCY


def acquire_screenshot_job_slot():
    global SCREENSHOT_JOB_ACTIVE_COUNT
    with SCREENSHOT_JOB_CONDITION:
        while SCREENSHOT_JOB_ACTIVE_COUNT >= screenshot_job_capacity():
            SCREENSHOT_JOB_CONDITION.wait(timeout=5)
        SCREENSHOT_JOB_ACTIVE_COUNT += 1
        logging.info(
            "screenshot job slot acquired: active=%s capacity=%s queued=%s",
            SCREENSHOT_JOB_ACTIVE_COUNT,
            screenshot_job_capacity(),
            screenshot_job_backlog_count(),
        )


def release_screenshot_job_slot():
    global SCREENSHOT_JOB_ACTIVE_COUNT
    with SCREENSHOT_JOB_CONDITION:
        SCREENSHOT_JOB_ACTIVE_COUNT = max(0, SCREENSHOT_JOB_ACTIVE_COUNT - 1)
        SCREENSHOT_JOB_CONDITION.notify_all()


def run_screenshot_job_async(job):

    def target():
        acquire_screenshot_job_slot()
        try:
            begin_screenshot_job_run(job)
            attempts = max(1, JOB_AUTO_RETRY_ATTEMPTS + 1)

            for attempt in range(1, attempts + 1):

                try:

                    if attempt > 1:

                        set_screenshot_job_progress(

                            job,

                            status="queued",

                            progress=max(2, clamp_progress(job.get("progress", 0))),

                            detail="任务失败，开始自动重试（第 %d/%d 次）" % (attempt, attempts),

                        )

                    process_screenshot_job(job)

                    return

                except Exception as exc:

                    logging.exception("screenshot job failed: %s", job["job_id"])

                    if attempt < attempts and should_auto_retry_job(exc):

                        continue

                    job["status"] = "failed"

                    job["progress"] = clamp_progress(job.get("progress", 0))
                    finish_screenshot_job_run(job)

                    message = str(exc).strip() or exc.__class__.__name__

                    trace = traceback.format_exc(limit=8)

                    job["error_message"] = "%s\n%s" % (message, trace)

                    upsert_screenshot_job_record(job)

                    failure_job = fetch_screenshot_job_row(job["job_id"]) or job
                    notify_screenshot_failure(failure_job, message)

                    return
        finally:
            release_screenshot_job_slot()



    thread = threading.Thread(target=target, name="screenshot-job-%s" % job["job_id"][:8])

    thread.daemon = True

    thread.start()





def retry_screenshot_job(job_id):
    job = fetch_screenshot_job_row(job_id)
    if not job:
        raise ValueError("\u4efb\u52a1\u4e0d\u5b58\u5728")
    if job.get("status") not in ("done", "failed"):
        raise ValueError("\u4efb\u52a1\u6b63\u5728\u5904\u7406\u4e2d\uff0c\u6682\u4e0d\u80fd\u91cd\u65b0\u5236\u4f5c")
    clear_screenshot_job_deleted_marker(job_id)
    force_remake = job.get("status") == "done"
    assets = job.get("assets", {})
    if not isinstance(assets, dict):
        assets = {}
    if force_remake:
        for spec in SCREENSHOT_SPECS:
            job[spec["field"]] = ""
            assets.pop(spec["key"], None)
            for path in (
                os.path.join(SCREENSHOT_WORK_ROOT, job_id, "generated", spec["filename"]),
                os.path.join(SCREENSHOT_PUBLIC_ROOT, job_id, spec["filename"]),
            ):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    logging.warning("failed to remove screenshot output before remake: %s", path)
        preserved_count = 0
        retry_count = len(SCREENSHOT_SPECS)
        progress_detail = "\u5df2\u6e05\u7a7a\u5b8c\u6210\u4efb\u52a1\u7684\u65e7\u622a\u56fe\uff0c\u5c06\u91cd\u65b0\u5236\u4f5c %d \u4e2a\u5c3a\u5bf8" % retry_count
    else:
        preserved_count = sum(
            1 for spec in SCREENSHOT_SPECS
            if str(job.get(spec["field"], "") or "").strip()
        )
        retry_count = max(0, len(SCREENSHOT_SPECS) - preserved_count)
        progress_detail = (
            "\u5df2\u4fdd\u7559 %d \u4e2a\u5df2\u751f\u6210\u5c3a\u5bf8\uff0c\u4ec5\u91cd\u65b0\u5236\u4f5c %d \u4e2a\u5931\u8d25\u6216\u7f3a\u5931\u5c3a\u5bf8"
            % (preserved_count, retry_count)
        )
    job["assets"] = assets
    job["status"] = "queued"
    job["progress"] = 2
    job["progress_detail"] = progress_detail
    job["error_message"] = ""
    job["started_at"] = ""
    job["finished_at"] = ""
    job["elapsed_seconds"] = 0
    job["token_total"] = 0
    job["token_usage"] = {}
    upsert_screenshot_job_record(job)
    run_screenshot_job_async(job)
    return {
        "job_id": job_id,
        "resumed": True,
        "force_remake": force_remake,
        "preserved_count": preserved_count,
        "retry_count": retry_count,
    }


def parse_screenshot_job_route(path):

    match = re.match(r"^/api/drama-screenshot-material/jobs/([0-9a-f]{32})(?:/(retry))?$", path)

    if not match:

        return None, None

    return match.group(1), match.group(2)





AD_MATERIAL_TASK_TYPE_ITERATION = "素材迭代——根据老素材效果优化"
AD_MATERIAL_TASK_TYPE_REFERENCE = "参考衍生——解析参考素材风格出新素材"
AD_MATERIAL_TASK_TYPE_COMPETITOR = "竞品借鉴——参考竞品优质素材"
AD_MATERIAL_TASK_TYPE_PLANNING = "综合策划——综合内外素材出方案"
AD_MATERIAL_TASK_TYPES = (
    AD_MATERIAL_TASK_TYPE_ITERATION,
    AD_MATERIAL_TASK_TYPE_REFERENCE,
    AD_MATERIAL_TASK_TYPE_COMPETITOR,
    AD_MATERIAL_TASK_TYPE_PLANNING,
)
AD_MATERIAL_TASK_TYPE_ALIASES = {
    "素材优化": AD_MATERIAL_TASK_TYPE_ITERATION,
    "素材迭代": AD_MATERIAL_TASK_TYPE_ITERATION,
    "参考衍生": AD_MATERIAL_TASK_TYPE_REFERENCE,
    "参考复刻": AD_MATERIAL_TASK_TYPE_REFERENCE,
    "参考素材": AD_MATERIAL_TASK_TYPE_REFERENCE,
    "竞品借鉴": AD_MATERIAL_TASK_TYPE_COMPETITOR,
    "综合策划": AD_MATERIAL_TASK_TYPE_PLANNING,
}
AD_MATERIAL_SIZE_OPTIONS = ("1:1", "4:5", "9:16", "1.91:1", "16:9")
AD_MATERIAL_SIZE_DIMENSIONS = {
    "1:1": "1080x1080",
    "4:5": "1080x1350",
    "9:16": "1080x1920",
    "1.91:1": "1200x628",
    "16:9": "1920x1080",
}
AD_MATERIAL_COMPETITOR_SOURCES = ("有米云", "metapi", "广大大")
AD_MATERIAL_STATUS_LABELS = {
    "draft": "待发布",
    "generating_demand": "生成需求中",
    "demand_review": "需求待审核",
    "demand_returned": "需求打回",
    "generating_material": "生成素材中",
    "material_review": "素材待审核",
    "material_returned": "素材打回",
    "material_abandoned": "已废弃",
    "done": "已完成",
    "failed": "失败",
}
AD_MATERIAL_ASSET_STATUS_LABELS = {
    "generating": "生成中",
    "pending_review": "待审核",
    "approved": "已通过",
    "rejected": "已驳回",
    "abandoned": "已废弃",
    "regenerating": "重新生成中",
    "uploaded": "已上报",
    "upload_failed": "上报失败",
}

AD_MATERIAL_TASK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ad_material_task (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL UNIQUE,
  task_type TEXT NOT NULL,
  competitor_source TEXT NOT NULL DEFAULT '',
  app_id TEXT NOT NULL,
  product_name TEXT NOT NULL DEFAULT '',
  country TEXT NOT NULL,
  language TEXT NOT NULL,
  size TEXT NOT NULL DEFAULT '',
  tag_name TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  store_url TEXT NOT NULL DEFAULT '',
  package_name TEXT NOT NULL DEFAULT '',
  product_icon_url TEXT NOT NULL DEFAULT '',
  quantity INTEGER NOT NULL DEFAULT 1,
  reference_files_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'draft',
  demand_text TEXT NOT NULL DEFAULT '',
  demand_artifacts_json TEXT NOT NULL DEFAULT '{}',
  review_reason TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  creator_user_id TEXT NOT NULL DEFAULT '',
  creator_open_id TEXT NOT NULL DEFAULT '',
  creator_email TEXT NOT NULL DEFAULT '',
  creator_name TEXT NOT NULL DEFAULT '',
  initiator_sub_user_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

AD_MATERIAL_ASSET_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ad_material_asset (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id TEXT NOT NULL UNIQUE,
  task_id TEXT NOT NULL,
  asset_index INTEGER NOT NULL DEFAULT 1,
  name TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  local_path TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'generating',
  review_reason TEXT NOT NULL DEFAULT '',
  source_api_id TEXT NOT NULL DEFAULT '',
  source_api_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

AD_MATERIAL_COMPETITOR_SOURCE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ad_material_competitor_source (
  source TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'active',
  fail_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  disabled_at TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def ensure_ad_material_tables():
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(AD_MATERIAL_TASK_TABLE_SQL)
            conn.execute(AD_MATERIAL_ASSET_TABLE_SQL)
            conn.execute(AD_MATERIAL_COMPETITOR_SOURCE_TABLE_SQL)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_material_task_status_updated ON ad_material_task(status, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_material_task_creator ON ad_material_task(creator_user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_material_asset_task ON ad_material_asset(task_id, asset_index)")
            for source in AD_MATERIAL_COMPETITOR_SOURCES:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ad_material_competitor_source (
                      source, status, fail_count, last_error, disabled_at, created_at, updated_at
                    ) VALUES (?, 'active', 0, '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (source,),
                )
            columns = [row["name"] for row in conn.execute("PRAGMA table_info(ad_material_task)").fetchall()]
            if "demand_artifacts_json" not in columns:
                conn.execute("ALTER TABLE ad_material_task ADD COLUMN demand_artifacts_json TEXT NOT NULL DEFAULT '{}'")
            if "store_url" not in columns:
                conn.execute("ALTER TABLE ad_material_task ADD COLUMN store_url TEXT NOT NULL DEFAULT ''")
            if "package_name" not in columns:
                conn.execute("ALTER TABLE ad_material_task ADD COLUMN package_name TEXT NOT NULL DEFAULT ''")
            if "product_icon_url" not in columns:
                conn.execute("ALTER TABLE ad_material_task ADD COLUMN product_icon_url TEXT NOT NULL DEFAULT ''")
            conn.commit()
        finally:
            conn.close()


def ad_material_task_work_dir(task_id):
    return os.path.join(AD_MATERIAL_WORK_ROOT, task_id)


def ad_material_public_dir(task_id):
    return os.path.join(AD_MATERIAL_PUBLIC_ROOT, task_id)


def normalize_ad_material_status(status):
    status = str(status or "").strip()
    return status if status in AD_MATERIAL_STATUS_LABELS else "draft"


def normalize_ad_material_task_type(value):
    value = str(value or "").strip()
    if value in AD_MATERIAL_TASK_TYPES:
        return value
    if value in AD_MATERIAL_TASK_TYPE_ALIASES:
        return AD_MATERIAL_TASK_TYPE_ALIASES[value]
    prefix = re.split(r"[—-]", value, 1)[0].strip()
    if prefix in AD_MATERIAL_TASK_TYPE_ALIASES:
        return AD_MATERIAL_TASK_TYPE_ALIASES[prefix]
    if value not in AD_MATERIAL_TASK_TYPES:
        raise StructuredApiError("invalid_task_type", "任务类型无效")
    return value


def ad_material_task_kind(value):
    try:
        task_type = normalize_ad_material_task_type(value)
    except Exception:
        task_type = str(value or "").strip()
    if task_type == AD_MATERIAL_TASK_TYPE_ITERATION:
        return "iteration"
    if task_type == AD_MATERIAL_TASK_TYPE_REFERENCE:
        return "reference"
    if task_type == AD_MATERIAL_TASK_TYPE_COMPETITOR:
        return "competitor"
    if task_type == AD_MATERIAL_TASK_TYPE_PLANNING:
        return "planning"
    return ""


def normalize_ad_material_size_label(value):
    text = str(value or "").strip().lower().replace(" ", "")
    text = text.replace("：", ":").replace("×", "x").replace("*", "x")
    dimension_map = {
        "1080x1080": "1:1",
        "1200x1200": "1:1",
        "1080x1350": "4:5",
        "1200x1500": "4:5",
        "1080x1920": "9:16",
        "1200x628": "1.91:1",
        "1200x630": "1.91:1",
        "1920x1080": "16:9",
        "1280x720": "16:9",
    }
    if text in dimension_map:
        return dimension_map[text]
    for option in AD_MATERIAL_SIZE_OPTIONS:
        if text == option.lower():
            return option
    raise StructuredApiError("invalid_size", "尺寸仅支持：%s" % " / ".join(AD_MATERIAL_SIZE_OPTIONS))


def parse_ad_material_size_plan_text(value, fallback_quantity=1):
    text = str(value or "").strip()
    fallback_quantity = max(1, int(fallback_quantity or 1))
    if not text:
        return [{"size": "1:1", "count": fallback_quantity}]
    parts = [part.strip() for part in re.split(r"[,，;；\n]+", text) if part.strip()]
    plan = []
    for part in parts:
        size = ""
        for option in sorted(AD_MATERIAL_SIZE_OPTIONS, key=len, reverse=True):
            if option.lower() in part.lower().replace("：", ":"):
                size = option
                break
        if not size:
            try:
                size = normalize_ad_material_size_label(part)
            except Exception:
                continue
        count = 1
        count_match = re.search(r"(?:x|×|\*)\s*(\d+)|(\d+)\s*(?:张|条|个)", part, flags=re.I)
        if count_match:
            count = int(next(group for group in count_match.groups() if group))
        elif len(parts) == 1:
            count = fallback_quantity
        if count > 0:
            plan.append({"size": size, "count": count})
    if not plan:
        return [{"size": "1:1", "count": fallback_quantity}]
    return merge_ad_material_size_plan(plan)


def merge_ad_material_size_plan(plan):
    counts = {size: 0 for size in AD_MATERIAL_SIZE_OPTIONS}
    for item in plan or []:
        size = normalize_ad_material_size_label(item.get("size") if isinstance(item, dict) else item)
        count = int((item.get("count") if isinstance(item, dict) else 1) or 0)
        if count > 0:
            counts[size] += count
    return [{"size": size, "count": counts[size]} for size in AD_MATERIAL_SIZE_OPTIONS if counts[size] > 0]


def normalize_ad_material_size_plan(payload, existing=None):
    payload = payload or {}
    fallback_quantity = int(payload.get("quantity", existing.get("quantity") if existing else 1) or 1)
    raw_plan = payload.get("size_plan")
    if isinstance(raw_plan, list):
        plan = merge_ad_material_size_plan(raw_plan)
    elif raw_plan:
        plan = parse_ad_material_size_plan_text(raw_plan, fallback_quantity)
    else:
        plan = parse_ad_material_size_plan_text(
            payload.get("size", existing.get("size") if existing else ""),
            fallback_quantity,
        )
    total = sum(int(item["count"]) for item in plan)
    if total < 1 or total > 20:
        raise StructuredApiError("invalid_quantity", "任务数量必须为1到20")
    return plan


def format_ad_material_size_plan(plan):
    normalized = merge_ad_material_size_plan(plan)
    return ", ".join("%s x %s" % (item["size"], item["count"]) for item in normalized)


def ad_material_size_plan_from_task(task):
    raw_plan = task.get("size_plan") if isinstance(task, dict) else None
    if isinstance(raw_plan, list) and raw_plan:
        try:
            return merge_ad_material_size_plan(raw_plan)
        except Exception:
            pass
    return parse_ad_material_size_plan_text(task.get("size"), task.get("quantity") or 1)


def ad_material_asset_size(task, index):
    current = 0
    for item in ad_material_size_plan_from_task(task):
        current += int(item["count"])
        if int(index or 1) <= current:
            return item["size"]
    plan = ad_material_size_plan_from_task(task)
    return plan[-1]["size"] if plan else "1:1"


def ad_material_asset_output_size(task, index):
    return AD_MATERIAL_SIZE_DIMENSIONS.get(ad_material_asset_size(task, index), "1080x1080")


def list_ad_material_competitor_sources(include_disabled=False):
    default_items = [
        {"source": source, "name": source, "status": "active", "fail_count": 0, "last_error": "", "disabled_at": ""}
        for source in AD_MATERIAL_COMPETITOR_SOURCES
    ]
    try:
        with JOB_DB_LOCK:
            conn = get_job_db_connection()
            try:
                rows = conn.execute(
                    """
                    SELECT source, status, fail_count, last_error, disabled_at
                    FROM ad_material_competitor_source
                    ORDER BY CASE source WHEN '有米云' THEN 1 WHEN 'metapi' THEN 2 WHEN '广大大' THEN 3 ELSE 99 END, source
                    """
                ).fetchall()
            finally:
                conn.close()
        items = []
        known = set(AD_MATERIAL_COMPETITOR_SOURCES)
        for row in rows:
            source = str(row["source"] or "").strip()
            if source not in known:
                continue
            status = str(row["status"] or "active").strip() or "active"
            if not include_disabled and status != "active":
                continue
            items.append({
                "source": source,
                "name": source,
                "status": status,
                "fail_count": int(row["fail_count"] or 0),
                "last_error": str(row["last_error"] or ""),
                "disabled_at": str(row["disabled_at"] or ""),
            })
        return items if rows else default_items
    except Exception:
        logging.exception("failed to list ad material competitor sources")
        return default_items


def active_ad_material_competitor_sources():
    return [item["source"] for item in list_ad_material_competitor_sources(include_disabled=False) if item.get("status") == "active"]


def normalize_competitor_source(task_type, value):
    value = str(value or "").strip()
    if ad_material_task_kind(task_type) in ("iteration", "reference"):
        return ""
    active_sources = active_ad_material_competitor_sources()
    if not active_sources:
        raise StructuredApiError("competitor_source_unavailable", "暂无可用竞品查询接口，请先恢复竞品源")
    if not value:
        return "有米云" if "有米云" in active_sources else active_sources[0]
    if value not in AD_MATERIAL_COMPETITOR_SOURCES:
        raise StructuredApiError("invalid_competitor_source", "竞品查询接口无效")
    if value not in active_sources:
        raise StructuredApiError("competitor_source_disabled", "该竞品查询接口已临时下架，请选择其他竞品源")
    return value


def session_is_admin(session):
    return bool(session and session.get("role") == "admin")


def ad_material_actor(session):
    session = session or {}
    return {
        "user_id": str(session.get("user_id", "") or ""),
        "open_id": str(session.get("open_id", "") or ""),
        "email": str(session.get("email", "") or ""),
        "name": str(session.get("name", "") or session.get("user_id", "") or "unknown"),
    }


def mysql_table_columns(table_name, database=None):
    database = database or ADMIN_MAPPING_MYSQL_DATABASE or DB_NAME
    if not database:
        return set()
    try:
        rows = run_mysql("SHOW COLUMNS FROM `%s`.`%s`" % (database.replace("`", "``"), table_name.replace("`", "``")))
        return {row[0] for row in rows if row}
    except FileNotFoundError:
        logging.warning("mysql client unavailable while reading columns: %s", table_name)
        return set()
    except Exception:
        logging.exception("failed to read mysql columns: %s", table_name)
        return set()


def sql_identifier(name):
    return "`%s`" % str(name or "").replace("`", "``")


AD_CONTROL_DB_NAME = os.environ.get("AD_CONTROL_DB_NAME", DB_NAME).strip() or "kunlunads_dev"
AD_CONTROL_GRAPH_VERSION = os.environ.get("AD_CONTROL_GRAPH_VERSION", "v19.0").strip() or "v19.0"
AD_CONTROL_GRAPH_TIMEOUT = int(os.environ.get("AD_CONTROL_GRAPH_TIMEOUT", "30"))
AD_CONTROL_PREVIEW_TTL_SECONDS = int(os.environ.get("AD_CONTROL_PREVIEW_TTL_SECONDS", "1800"))
AD_CONTROL_MAX_PAGE_SIZE = int(os.environ.get("AD_CONTROL_MAX_PAGE_SIZE", "200"))
AD_CONTROL_MAX_EXECUTE = int(os.environ.get("AD_CONTROL_MAX_EXECUTE", "50"))
AD_CONTROL_MAX_METRIC_IDS = int(os.environ.get("AD_CONTROL_MAX_METRIC_IDS", "200"))
AD_CONTROL_MAX_LIVE_ACCOUNTS = int(os.environ.get("AD_CONTROL_MAX_LIVE_ACCOUNTS", "40"))
AD_CONTROL_MAX_LIVE_CAMPAIGNS = int(os.environ.get("AD_CONTROL_MAX_LIVE_CAMPAIGNS", "1000"))
AD_CONTROL_MAX_LIVE_EXECUTE = int(os.environ.get("AD_CONTROL_MAX_LIVE_EXECUTE", "200"))
AD_CONTROL_LIVE_MAX_WORKERS = int(os.environ.get("AD_CONTROL_LIVE_MAX_WORKERS", "12"))
AD_CONTROL_RESOURCE_LIMIT_PERCENT = float(os.environ.get("AD_CONTROL_RESOURCE_LIMIT_PERCENT", "85"))
AD_CONTROL_REDIS_URL = os.environ.get("AD_CONTROL_REDIS_URL", "").strip()
AD_CONTROL_INSIGHT_START_TABLE = os.environ.get("AD_CONTROL_INSIGHT_START_TABLE", "ads_facebook_hours_insights").strip() or "ads_facebook_hours_insights"
AD_CONTROL_INSIGHT_START_FIELD = os.environ.get("AD_CONTROL_INSIGHT_START_FIELD", "dt").strip() or "dt"
AD_CONTROL_INSIGHT_CAMPAIGN_FIELD = os.environ.get("AD_CONTROL_INSIGHT_CAMPAIGN_FIELD", "campaign_id").strip() or "campaign_id"
AD_CONTROL_INSIGHT_ACCOUNT_FIELD = os.environ.get("AD_CONTROL_INSIGHT_ACCOUNT_FIELD", "ad_account_id").strip()
AD_CONTROL_INSIGHT_PRODUCT_FIELD = os.environ.get("AD_CONTROL_INSIGHT_PRODUCT_FIELD", "").strip()
AD_CONTROL_INSIGHTS_TIME_INCREMENT = os.environ.get("AD_CONTROL_INSIGHTS_TIME_INCREMENT", "1").strip() or "1"

AD_CONTROL_LEVELS = {
    "campaign": {
        "id_column": "campaign_id",
        "name_column": "campaign_name",
        "action_at_column": "campaign_action_at",
        "label": "Campaign",
    },
    "adset": {
        "id_column": "adset_id",
        "name_column": "adset_name",
        "action_at_column": "adset_action_at",
        "label": "Ad set",
    },
    "ad": {
        "id_column": "ad_id",
        "name_column": "ad_name",
        "action_at_column": "ad_action_at",
        "label": "Ad",
    },
}

AD_CONTROL_PREVIEW_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ad_control_preview (
  preview_id TEXT PRIMARY KEY,
  actor_user_id TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL DEFAULT '',
  level TEXT NOT NULL DEFAULT 'campaign',
  product TEXT NOT NULL DEFAULT '',
  criteria_json TEXT NOT NULL DEFAULT '{}',
  sample_json TEXT NOT NULL DEFAULT '[]',
  total_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TEXT NOT NULL DEFAULT ''
)
"""

AD_CONTROL_ACTION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ad_control_action (
  action_id TEXT PRIMARY KEY,
  preview_id TEXT NOT NULL DEFAULT '',
  rule_id TEXT NOT NULL DEFAULT '',
  actor_user_id TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL DEFAULT '',
  level TEXT NOT NULL DEFAULT 'campaign',
  product TEXT NOT NULL DEFAULT '',
  criteria_json TEXT NOT NULL DEFAULT '{}',
  requested_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  error_count INTEGER NOT NULL DEFAULT 0,
  dry_run INTEGER NOT NULL DEFAULT 0,
  results_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

AD_CONTROL_OBJECT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ad_control_object_state (
  object_key TEXT PRIMARY KEY,
  product TEXT NOT NULL DEFAULT '',
  level TEXT NOT NULL DEFAULT 'campaign',
  account_id TEXT NOT NULL DEFAULT '',
  object_id TEXT NOT NULL DEFAULT '',
  campaign_id TEXT NOT NULL DEFAULT '',
  last_pause_action_id TEXT NOT NULL DEFAULT '',
  last_reopen_action_id TEXT NOT NULL DEFAULT '',
  object_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT '',
  paused_at TEXT NOT NULL DEFAULT '',
  reopened_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

AD_CONTROL_RULE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ad_control_rule (
  rule_id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 0,
  product TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL DEFAULT 'pause',
  level TEXT NOT NULL DEFAULT 'campaign',
  criteria_json TEXT NOT NULL DEFAULT '{}',
  schedule_json TEXT NOT NULL DEFAULT '{}',
  thresholds_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_run_at TEXT NOT NULL DEFAULT '',
  last_result_json TEXT NOT NULL DEFAULT '{}'
)
"""

AD_CONTROL_TOKEN_CONFIG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ad_control_token_config (
  product TEXT NOT NULL DEFAULT '',
  account_id TEXT NOT NULL DEFAULT '',
  user_id TEXT NOT NULL DEFAULT '',
  label TEXT NOT NULL DEFAULT '',
  validation_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (product, account_id)
)
"""

AD_CONTROL_ACCOUNT_GROUP_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ad_control_account_group (
  group_id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  product TEXT NOT NULL DEFAULT '',
  account_ids_json TEXT NOT NULL DEFAULT '[]',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  deleted INTEGER NOT NULL DEFAULT 0
)
"""

AD_CONTROL_RULE_SET_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ad_control_rule_set (
  rule_set_id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  product TEXT NOT NULL DEFAULT '',
  rules_json TEXT NOT NULL DEFAULT '[]',
  default_window_json TEXT NOT NULL DEFAULT '{"type":"since_start"}',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  deleted INTEGER NOT NULL DEFAULT 0
)
"""

AD_CONTROL_RULE_GROUP_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ad_control_rule_group (
  group_id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  product TEXT NOT NULL DEFAULT '',
  rule_set_id TEXT NOT NULL DEFAULT '',
  account_group_id TEXT NOT NULL DEFAULT '',
  account_ids_json TEXT NOT NULL DEFAULT '[]',
  rules_json TEXT NOT NULL DEFAULT '[]',
  strategy_json TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 0,
  emergency_stopped INTEGER NOT NULL DEFAULT 0,
  last_preview_id TEXT NOT NULL DEFAULT '',
  last_preview_hash TEXT NOT NULL DEFAULT '',
  last_run_at TEXT NOT NULL DEFAULT '',
  last_result_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  deleted INTEGER NOT NULL DEFAULT 0
)
"""


def ensure_ad_control_tables():
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(AD_CONTROL_PREVIEW_TABLE_SQL)
            conn.execute(AD_CONTROL_ACTION_TABLE_SQL)
            conn.execute(AD_CONTROL_OBJECT_TABLE_SQL)
            conn.execute(AD_CONTROL_RULE_TABLE_SQL)
            conn.execute(AD_CONTROL_TOKEN_CONFIG_TABLE_SQL)
            conn.execute(AD_CONTROL_ACCOUNT_GROUP_TABLE_SQL)
            conn.execute(AD_CONTROL_RULE_SET_TABLE_SQL)
            conn.execute(AD_CONTROL_RULE_GROUP_TABLE_SQL)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(ad_control_rule_group)").fetchall()}
            if "rule_set_id" not in columns:
                conn.execute("ALTER TABLE ad_control_rule_group ADD COLUMN rule_set_id TEXT NOT NULL DEFAULT ''")
            if "strategy_json" not in columns:
                conn.execute("ALTER TABLE ad_control_rule_group ADD COLUMN strategy_json TEXT NOT NULL DEFAULT '{}'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_control_preview_expires ON ad_control_preview(expires_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_control_action_created ON ad_control_action(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_control_object_product ON ad_control_object_state(product, level, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_control_rule_enabled ON ad_control_rule(enabled, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_control_account_group_product ON ad_control_account_group(product, deleted)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_control_rule_set_product ON ad_control_rule_set(product, deleted)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_control_rule_group_product ON ad_control_rule_group(product, deleted, enabled)")
            legacy_rows = conn.execute(
                """
                SELECT group_id, name, product, rules_json, created_by, created_at, updated_at
                  FROM ad_control_rule_group
                 WHERE COALESCE(rule_set_id, '') = ''
                """
            ).fetchall()
            for row in legacy_rows:
                rule_set_id = "legacy_%s" % str(row["group_id"] or "").strip()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ad_control_rule_set (
                      rule_set_id, name, product, rules_json, default_window_json,
                      created_by, created_at, updated_at, deleted
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        rule_set_id,
                        row["name"] or "",
                        row["product"] or "",
                        row["rules_json"] or "[]",
                        '{"type":"since_start"}',
                        row["created_by"] or "",
                        row["created_at"] or "",
                        row["updated_at"] or "",
                    ),
                )
                conn.execute(
                    "UPDATE ad_control_rule_group SET rule_set_id=? WHERE group_id=?",
                    (rule_set_id, row["group_id"]),
                )
            conn.commit()
        finally:
            conn.close()


def ad_control_db_prefix():
    return sql_identifier(AD_CONTROL_DB_NAME)


def ad_control_table(table_name):
    return "%s.%s" % (ad_control_db_prefix(), sql_identifier(table_name))


def ad_control_quote(value):
    return "'%s'" % mysql_escape_literal(value)


def ad_control_sql_in(values):
    clean = [str(value or "").strip() for value in values if str(value or "").strip()]
    if not clean:
        return "('')"
    return "(" + ",".join(ad_control_quote(value) for value in clean) + ")"


def ad_control_norm_account_sql(expr):
    return "REPLACE(REPLACE(TRIM(%s),'act_',''),'ACT_','')" % expr


def ad_control_normalize_account(value):
    return str(value or "").strip().replace("act_", "").replace("ACT_", "")


def ad_control_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = re.split(r"[,，\s]+", str(value or ""))
    out = []
    seen = set()
    for item in raw:
        text = str(item or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def ad_control_level(value):
    level = str(value or "campaign").strip().lower()
    if level not in AD_CONTROL_LEVELS:
        raise StructuredApiError("invalid_level", "对象层级无效")
    return level


def ad_control_action(value):
    action = str(value or "preview").strip().lower()
    if action in ("close", "pause", "paused"):
        return "pause"
    if action in ("open", "reopen", "active", "restart"):
        return "reopen"
    if action == "preview":
        return "preview"
    raise StructuredApiError("invalid_action", "调控动作无效")


def ad_control_int(value, default=0, minimum=None, maximum=None):
    try:
        number = int(value)
    except Exception:
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def ad_control_criteria(payload, require_action=False):
    payload = payload or {}
    product = str(payload.get("product", "") or "").strip()
    if not product:
        raise StructuredApiError("missing_product", "请选择产品")
    action = ad_control_action(payload.get("action", "preview"))
    if require_action and action == "preview":
        raise StructuredApiError("missing_action", "请选择关停或重启动作")
    level = ad_control_level(payload.get("level", "campaign"))
    page_size = ad_control_int(payload.get("page_size", 50), 50, 1, AD_CONTROL_MAX_PAGE_SIZE)
    page = ad_control_int(payload.get("page", 1), 1, 1, 100000)
    criteria = {
        "product": product,
        "action": action,
        "level": level,
        "accounts": ad_control_list(payload.get("accounts") or payload.get("account_ids")),
        "timezones": ad_control_list(payload.get("timezones")),
        "countries": [item.upper() for item in ad_control_list(payload.get("countries"))],
        "languages": [item.lower() for item in ad_control_list(payload.get("languages"))],
        "statuses": [item.upper() for item in ad_control_list(payload.get("statuses"))],
        "query": str(payload.get("query", "") or "").strip(),
        "created_from": str(payload.get("created_from", "") or "").strip(),
        "created_to": str(payload.get("created_to", "") or "").strip(),
        "metric_days": ad_control_int(payload.get("metric_days", 3), 3, 1, 30),
        "page": page,
        "page_size": page_size,
    }
    return criteria


def ad_control_candidate_where(criteria):
    level_cfg = AD_CONTROL_LEVELS[criteria["level"]]
    id_column = level_cfg["id_column"]
    where = [
        "d.product=%s" % ad_control_quote(criteria["product"]),
        "d.%s IS NOT NULL" % sql_identifier(id_column),
        "d.%s<>''" % sql_identifier(id_column),
    ]
    accounts = [ad_control_normalize_account(item) for item in criteria.get("accounts") or []]
    if accounts:
        where.append("%s IN %s" % (ad_control_norm_account_sql("d.ad_account_id"), ad_control_sql_in(accounts)))
    if criteria.get("timezones"):
        where.append("CAST(s.time_zone AS CHAR) IN %s" % ad_control_sql_in(criteria["timezones"]))
    if criteria.get("countries"):
        where.append("UPPER(COALESCE(d.country,'')) IN %s" % ad_control_sql_in(criteria["countries"]))
    if criteria.get("languages"):
        where.append("LOWER(COALESCE(d.language,'')) IN %s" % ad_control_sql_in(criteria["languages"]))
    if criteria.get("statuses"):
        where.append("UPPER(COALESCE(d.status,'')) IN %s" % ad_control_sql_in(criteria["statuses"]))
    if criteria.get("created_from"):
        where.append("d.created_at >= %s" % ad_control_quote(criteria["created_from"] + " 00:00:00"))
    if criteria.get("created_to"):
        where.append("d.created_at <= %s" % ad_control_quote(criteria["created_to"] + " 23:59:59"))
    if criteria.get("query"):
        like = "%%%s%%" % mysql_escape_literal(criteria["query"])
        text_columns = [
            "d.ad_account_id",
            "d.campaign_id",
            "d.campaign_name",
            "d.adset_id",
            "d.adset_name",
            "d.ad_id",
            "d.ad_name",
        ]
        where.append("(" + " OR ".join("%s LIKE '%s'" % (column, like) for column in text_columns) + ")")
    return " AND ".join(where)


def ad_control_candidate_join():
    return (
        "FROM {data} d "
        "LEFT JOIN {accounts} s ON s.platform_id=1 AND {account_norm}= {setting_norm}"
    ).format(
        data=ad_control_table("ads_facebook_auto_created_data"),
        accounts=ad_control_table("ads_accounts_setting"),
        account_norm=ad_control_norm_account_sql("d.ad_account_id"),
        setting_norm=ad_control_norm_account_sql("s.account_id"),
    )


def ad_control_group_columns(level):
    if level == "campaign":
        return ["d.ad_account_id", "d.campaign_id"]
    if level == "adset":
        return ["d.ad_account_id", "d.campaign_id", "d.adset_id"]
    return ["d.ad_account_id", "d.campaign_id", "d.adset_id", "d.ad_id"]


def ad_control_fetch_candidates(criteria, page=None, page_size=None):
    level = criteria["level"]
    level_cfg = AD_CONTROL_LEVELS[level]
    id_column = level_cfg["id_column"]
    name_column = level_cfg["name_column"]
    page = ad_control_int(page or criteria.get("page", 1), 1, 1, 100000)
    page_size = ad_control_int(page_size or criteria.get("page_size", 50), 50, 1, AD_CONTROL_MAX_PAGE_SIZE)
    where_sql = ad_control_candidate_where(criteria)
    join_sql = ad_control_candidate_join()
    group_sql = ", ".join(ad_control_group_columns(level))
    count_sql = (
        "SELECT COUNT(DISTINCT CONCAT_WS(':', d.ad_account_id, d.%s)) %s WHERE %s"
        % (sql_identifier(id_column), join_sql, where_sql)
    )
    total_rows = run_mysql(count_sql)
    total = int(total_rows[0][0] or 0) if total_rows else 0
    offset = (page - 1) * page_size
    select_sql = """
        SELECT
          MIN(d.id),
          COALESCE(MIN(d.product), ''),
          d.ad_account_id,
          COALESCE(MAX(NULLIF(s.name,'')), ''),
          COALESCE(MAX(CAST(s.time_zone AS CHAR)), ''),
          COALESCE(MAX(CAST(s.account_status AS CHAR)), ''),
          COALESCE(MAX(CAST(s.is_inactive AS CHAR)), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(NULLIF(d.campaign_id,'') ORDER BY d.updated_at DESC SEPARATOR '\\n'), '\\n', 1), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(NULLIF(d.campaign_name,'') ORDER BY d.updated_at DESC SEPARATOR '\\n'), '\\n', 1), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(NULLIF(d.adset_id,'') ORDER BY d.updated_at DESC SEPARATOR '\\n'), '\\n', 1), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(NULLIF(d.adset_name,'') ORDER BY d.updated_at DESC SEPARATOR '\\n'), '\\n', 1), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(NULLIF(d.ad_id,'') ORDER BY d.updated_at DESC SEPARATOR '\\n'), '\\n', 1), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(NULLIF(d.ad_name,'') ORDER BY d.updated_at DESC SEPARATOR '\\n'), '\\n', 1), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(NULLIF(d.{id_column},'') ORDER BY d.updated_at DESC SEPARATOR '\\n'), '\\n', 1), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(NULLIF(d.{name_column},'') ORDER BY d.updated_at DESC SEPARATOR '\\n'), '\\n', 1), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(NULLIF(d.status,'') ORDER BY d.updated_at DESC SEPARATOR ','), ',', 1), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(CAST(d.local_status AS CHAR) ORDER BY d.updated_at DESC SEPARATOR ','), ',', 1), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(NULLIF(d.country,'') ORDER BY d.updated_at DESC SEPARATOR ','), ',', 1), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(NULLIF(d.language,'') ORDER BY d.updated_at DESC SEPARATOR ','), ',', 1), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(CAST(d.budget AS CHAR) ORDER BY d.updated_at DESC SEPARATOR ','), ',', 1), '0'),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(CAST(d.latest_budget AS CHAR) ORDER BY d.updated_at DESC SEPARATOR ','), ',', 1), '0'),
          GROUP_CONCAT(DISTINCT CAST(d.user_id AS CHAR) ORDER BY d.user_id SEPARATOR ','),
          MIN(d.created_at),
          MAX(d.updated_at),
          COUNT(*)
        {join_sql}
        WHERE {where_sql}
        GROUP BY {group_sql}
        ORDER BY MAX(d.updated_at) DESC
        LIMIT {limit_value} OFFSET {offset_value}
    """.format(
        id_column=sql_identifier(id_column),
        name_column=sql_identifier(name_column),
        join_sql=join_sql,
        where_sql=where_sql,
        group_sql=group_sql,
        limit_value=page_size,
        offset_value=offset,
    )
    rows = run_mysql(" ".join(select_sql.split()))
    items = []
    keys = [
        "row_id", "product", "account_id", "account_name", "time_zone", "account_status", "is_inactive",
        "campaign_id", "campaign_name", "adset_id", "adset_name", "ad_id", "ad_name", "object_id",
        "object_name", "status", "local_status", "country", "language", "budget", "latest_budget",
        "user_ids", "created_at", "updated_at", "row_count",
    ]
    for raw in rows:
        item = {key: (raw[index] if index < len(raw) else "") for index, key in enumerate(keys)}
        item["level"] = level
        item["object_key"] = ad_control_object_key(item)
        item["status"] = str(item.get("status") or "").upper()
        item["account_normalized"] = ad_control_normalize_account(item.get("account_id"))
        try:
            item["row_count"] = int(item.get("row_count") or 0)
        except Exception:
            item["row_count"] = 0
        items.append(item)
    ad_control_attach_metrics(criteria, items)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def ad_control_attach_metrics(criteria, items):
    campaign_ids = []
    for item in items:
        campaign_id = str(item.get("campaign_id") or "").strip()
        if campaign_id and campaign_id not in campaign_ids:
            campaign_ids.append(campaign_id)
    campaign_ids = campaign_ids[:AD_CONTROL_MAX_METRIC_IDS]
    if not campaign_ids:
        return
    days = ad_control_int(criteria.get("metric_days", 3), 3, 1, 30)
    sql = """
        SELECT account_id, campaign_id, ROUND(SUM(spend_usd),2), COALESCE(SUM(install),0), COUNT(DISTINCT dt)
          FROM {table}
         WHERE product={product}
           AND campaign_id IN {campaign_ids}
           AND dt >= CURDATE() - INTERVAL {days} DAY
         GROUP BY account_id, campaign_id
    """.format(
        table=ad_control_table("ads_platform_report_items"),
        product=ad_control_quote(criteria["product"]),
        campaign_ids=ad_control_sql_in(campaign_ids),
        days=days,
    )
    try:
        rows = run_mysql(" ".join(sql.split()))
    except Exception:
        logging.exception("failed to load ad control metrics")
        return
    metrics = {}
    by_campaign = {}
    for row in rows:
        account_id = ad_control_normalize_account(row[0] if len(row) > 0 else "")
        campaign_id = str(row[1] if len(row) > 1 else "")
        payload = {
            "spend_usd": float(row[2] or 0),
            "install": int(float(row[3] or 0)),
            "metric_days": int(float(row[4] or 0)),
        }
        metrics[(account_id, campaign_id)] = payload
        by_campaign[campaign_id] = payload
    for item in items:
        account_id = ad_control_normalize_account(item.get("account_id"))
        campaign_id = str(item.get("campaign_id") or "")
        item["metrics"] = metrics.get((account_id, campaign_id)) or by_campaign.get(campaign_id) or {
            "spend_usd": 0,
            "install": 0,
            "metric_days": 0,
        }


def ad_control_object_key(item):
    return "%s:%s:%s:%s" % (
        str(item.get("product") or ""),
        str(item.get("level") or "campaign"),
        ad_control_normalize_account(item.get("account_id")),
        str(item.get("object_id") or ""),
    )


def list_ad_control_products(query="", limit=200):
    limit = ad_control_int(limit, 200, 1, 500)
    where = "(name<>'' OR product<>'')"
    if query:
        like = "%%%s%%" % mysql_escape_literal(query)
        where += " AND (name LIKE '%s' OR product LIKE '%s' OR app_id LIKE '%s')" % (like, like, like)
    sql = """
        SELECT name, product, app_id, updated_at
          FROM {table}
         WHERE {where}
         ORDER BY updated_at DESC
         LIMIT {limit}
    """.format(table=ad_control_table("setting_product"), where=where, limit=limit)
    rows = run_mysql(" ".join(sql.split()))
    items = []
    seen = set()
    for row in rows:
        name = str(row[0] or "").strip()
        product_value = str(row[1] or "").strip()
        app_id = str(row[2] or "").strip()
        updated_at = row[3] if len(row) > 3 else ""
        product_key = product_value or name
        if not product_key or product_key in seen:
            continue
        seen.add(product_key)
        label_parts = []
        for value in (name, product_value, app_id):
            if value and value not in label_parts:
                label_parts.append(value)
        items.append({
            "product": product_key,
            "label": " / ".join(label_parts) if label_parts else product_key,
            "name": name,
            "product_value": product_value,
            "app_id": app_id,
            "account_count": "",
            "campaign_count": "",
            "updated_at": updated_at,
        })
        if len(items) >= limit:
            break
    return {"items": items}


def list_ad_control_accounts(product):
    product = str(product or "").strip()
    if not product:
        raise StructuredApiError("missing_product", "请选择产品")
    sql = """
        SELECT
          d.ad_account_id,
          COALESCE(MAX(NULLIF(s.name,'')), ''),
          COALESCE(MAX(CAST(s.time_zone AS CHAR)), ''),
          COALESCE(MAX(CAST(s.account_status AS CHAR)), ''),
          COALESCE(MAX(CAST(s.is_inactive AS CHAR)), ''),
          COUNT(DISTINCT d.campaign_id),
          COUNT(DISTINCT d.adset_id),
          COUNT(DISTINCT d.ad_id),
          MAX(d.updated_at)
        FROM {data} d
        LEFT JOIN {accounts} s ON s.platform_id=1 AND {account_norm}= {setting_norm}
        WHERE d.product={product}
          AND d.ad_account_id IS NOT NULL
          AND d.ad_account_id<>''
        GROUP BY d.ad_account_id
        ORDER BY MAX(d.updated_at) DESC
        LIMIT 1000
    """.format(
        data=ad_control_table("ads_facebook_auto_created_data"),
        accounts=ad_control_table("ads_accounts_setting"),
        account_norm=ad_control_norm_account_sql("d.ad_account_id"),
        setting_norm=ad_control_norm_account_sql("s.account_id"),
        product=ad_control_quote(product),
    )
    rows = run_mysql(" ".join(sql.split()))
    items = []
    for row in rows:
        items.append({
            "account_id": row[0],
            "account_name": row[1],
            "time_zone": row[2],
            "account_status": row[3],
            "is_inactive": row[4],
            "campaign_count": int(row[5] or 0),
            "adset_count": int(row[6] or 0),
            "ad_count": int(row[7] or 0),
            "updated_at": row[8],
        })
    return {"items": items}


def create_ad_control_preview(payload, session):
    ensure_ad_control_tables()
    criteria = ad_control_criteria(payload, require_action=False)
    data = ad_control_fetch_candidates(criteria)
    preview_id = uuid.uuid4().hex
    expires_at = (datetime.utcnow() + timedelta(seconds=AD_CONTROL_PREVIEW_TTL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
    actor_user_id = str((session or {}).get("user_id") or "")
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO ad_control_preview (
                  preview_id, actor_user_id, action, level, product, criteria_json,
                  sample_json, total_count, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                """,
                (
                    preview_id,
                    actor_user_id,
                    criteria["action"],
                    criteria["level"],
                    criteria["product"],
                    json.dumps(criteria, ensure_ascii=False),
                    json.dumps(data["items"][: min(50, len(data["items"]))], ensure_ascii=False),
                    data["total"],
                    expires_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    data.update({
        "preview_id": preview_id,
        "expires_at": expires_at,
        "action": criteria["action"],
        "level": criteria["level"],
        "product": criteria["product"],
    })
    return data


def fetch_ad_control_preview(preview_id):
    preview_id = str(preview_id or "").strip()
    if not preview_id:
        raise StructuredApiError("missing_preview", "请先试算")
    ensure_ad_control_tables()
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            row = conn.execute("SELECT * FROM ad_control_preview WHERE preview_id = ?", (preview_id,)).fetchone()
        finally:
            conn.close()
    if not row:
        raise StructuredApiError("preview_not_found", "试算批次不存在或已失效")
    expires_at = str(row["expires_at"] or "")
    if expires_at:
        try:
            if datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S") < datetime.utcnow():
                raise StructuredApiError("preview_expired", "试算批次已过期，请重新试算")
        except StructuredApiError:
            raise
        except Exception:
            pass
    return dict(row)


def ad_control_token_for_user_ids(user_ids):
    ids = [item for item in ad_control_list(user_ids) if item and item != "0"]
    if not ids:
        return ""
    sql = """
        SELECT accessToken
          FROM {table}
         WHERE user_id IN {user_ids}
           AND accessToken IS NOT NULL
           AND accessToken<>''
         ORDER BY FIELD(CAST(user_id AS CHAR), {field_ids})
         LIMIT 1
    """.format(
        table=ad_control_table("ads_facebook_info"),
        user_ids=ad_control_sql_in(ids),
        field_ids=",".join(ad_control_quote(item) for item in ids),
    )
    rows = run_mysql(" ".join(sql.split()))
    return str(rows[0][0] or "").strip() if rows else ""


def ad_control_graph_get(token, object_id, fields):
    response = requests.get(
        "https://graph.facebook.com/%s/%s" % (AD_CONTROL_GRAPH_VERSION, object_id),
        params={"access_token": token, "fields": fields},
        timeout=AD_CONTROL_GRAPH_TIMEOUT,
    )
    payload = response.json() if response.content else {}
    if response.status_code >= 400 or payload.get("error"):
        raise RuntimeError(json.dumps(payload.get("error") or payload, ensure_ascii=False))
    return payload


def ad_control_graph_set_status(token, object_id, status):
    response = requests.post(
        "https://graph.facebook.com/%s/%s" % (AD_CONTROL_GRAPH_VERSION, object_id),
        data={"access_token": token, "status": status},
        timeout=AD_CONTROL_GRAPH_TIMEOUT,
    )
    payload = response.json() if response.content else {}
    if response.status_code >= 400 or payload.get("error"):
        raise RuntimeError(json.dumps(payload.get("error") or payload, ensure_ascii=False))
    return payload


def ad_control_meta_fields(level):
    if level == "campaign":
        return "account_id,status,effective_status,name"
    if level == "adset":
        return "account_id,campaign_id,status,effective_status,name"
    return "account_id,campaign_id,adset_id,status,effective_status,name"


def ad_control_update_business_status(row, target_status):
    level = row.get("level") or "campaign"
    level_cfg = AD_CONTROL_LEVELS[level]
    id_column = level_cfg["id_column"]
    action_column = level_cfg["action_at_column"]
    sql = """
        UPDATE {table}
           SET status={status},
               {action_column}=UNIX_TIMESTAMP(),
               updated_at=NOW()
         WHERE product={product}
           AND {account_norm}= {account_id}
           AND {id_column}={object_id}
    """.format(
        table=ad_control_table("ads_facebook_auto_created_data"),
        status=ad_control_quote(target_status),
        action_column=sql_identifier(action_column),
        product=ad_control_quote(row.get("product")),
        account_norm=ad_control_norm_account_sql("ad_account_id"),
        account_id=ad_control_quote(ad_control_normalize_account(row.get("account_id"))),
        id_column=sql_identifier(id_column),
        object_id=ad_control_quote(row.get("object_id")),
    )
    run_mysql(" ".join(sql.split()))


def ad_control_load_object_states(items):
    keys = [item.get("object_key") for item in items if item.get("object_key")]
    if not keys:
        return {}
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            placeholders = ",".join(["?"] * len(keys))
            rows = conn.execute(
                "SELECT * FROM ad_control_object_state WHERE object_key IN (%s)" % placeholders,
                keys,
            ).fetchall()
            return {row["object_key"]: dict(row) for row in rows}
        finally:
            conn.close()


def ad_control_save_object_state(action_id, row, status):
    now_text = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    existing = ad_control_load_object_states([row]).get(row["object_key"], {})
    pause_action = action_id if status == "paused" else existing.get("last_pause_action_id", "")
    reopen_action = action_id if status == "reopened" else existing.get("last_reopen_action_id", "")
    paused_at = now_text if status == "paused" else existing.get("paused_at", "")
    reopened_at = now_text if status == "reopened" else existing.get("reopened_at", "")
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO ad_control_object_state (
                  object_key, product, level, account_id, object_id, campaign_id,
                  last_pause_action_id, last_reopen_action_id, object_json,
                  status, paused_at, reopened_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    row["object_key"],
                    row.get("product", ""),
                    row.get("level", ""),
                    row.get("account_id", ""),
                    row.get("object_id", ""),
                    row.get("campaign_id", ""),
                    pause_action,
                    reopen_action,
                    json.dumps(row, ensure_ascii=False),
                    status,
                    paused_at,
                    reopened_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def execute_ad_control(payload, session):
    ensure_ad_control_tables()
    preview = fetch_ad_control_preview(payload.get("preview_id"))
    criteria = json.loads(preview["criteria_json"] or "{}")
    action = ad_control_action(payload.get("action") or criteria.get("action"))
    if action not in ("pause", "reopen"):
        raise StructuredApiError("invalid_action", "请选择关停或重启动作")
    if action != criteria.get("action"):
        raise StructuredApiError("preview_action_mismatch", "执行动作和试算动作不一致，请重新试算")
    max_items = ad_control_int(payload.get("max_items", AD_CONTROL_MAX_EXECUTE), AD_CONTROL_MAX_EXECUTE, 1, AD_CONTROL_MAX_EXECUTE)
    dry_run = bool(payload.get("dry_run"))
    criteria = dict(criteria)
    data = ad_control_fetch_candidates(criteria, page=1, page_size=max_items)
    items = data["items"]
    states = ad_control_load_object_states(items)
    action_id = uuid.uuid4().hex
    target_status = "PAUSED" if action == "pause" else "ACTIVE"
    results = []
    success_count = skipped_count = error_count = 0
    for item in items:
        item["level"] = criteria["level"]
        item["object_key"] = ad_control_object_key(item)
        if action == "reopen" and states.get(item["object_key"], {}).get("status") != "paused":
            skipped_count += 1
            results.append({"object_key": item["object_key"], "status": "skipped", "reason": "not_paused_by_control_center"})
            continue
        if action == "pause" and str(item.get("status") or "").upper() == "PAUSED":
            skipped_count += 1
            results.append({"object_key": item["object_key"], "status": "skipped", "reason": "already_paused"})
            continue
        try:
            token = ad_control_token_for_user_ids(item.get("user_ids", ""))
            if not token:
                skipped_count += 1
                results.append({"object_key": item["object_key"], "status": "skipped", "reason": "missing_meta_token"})
                continue
            meta = ad_control_graph_get(token, item["object_id"], ad_control_meta_fields(item["level"]))
            meta_account = ad_control_normalize_account(meta.get("account_id"))
            item_account = ad_control_normalize_account(item.get("account_id"))
            if meta_account and item_account and meta_account != item_account:
                skipped_count += 1
                results.append({
                    "object_key": item["object_key"],
                    "status": "skipped",
                    "reason": "account_owner_mismatch",
                    "meta_account": meta.get("account_id"),
                    "asset_account": item.get("account_id"),
                })
                continue
            if dry_run:
                success_count += 1
                results.append({"object_key": item["object_key"], "status": "dry_run", "meta": meta})
                continue
            payload_result = ad_control_graph_set_status(token, item["object_id"], target_status)
            ad_control_update_business_status(item, target_status)
            ad_control_save_object_state(action_id, item, "paused" if action == "pause" else "reopened")
            success_count += 1
            results.append({"object_key": item["object_key"], "status": "success", "meta": payload_result})
        except Exception as exc:
            error_count += 1
            logging.exception("ad control execute failed: %s", item.get("object_key"))
            results.append({"object_key": item.get("object_key", ""), "status": "error", "reason": str(exc)})
    actor_user_id = str((session or {}).get("user_id") or "")
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO ad_control_action (
                  action_id, preview_id, actor_user_id, action, level, product, criteria_json,
                  requested_count, success_count, skipped_count, error_count, dry_run,
                  results_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    action_id,
                    preview["preview_id"],
                    actor_user_id,
                    action,
                    criteria["level"],
                    criteria["product"],
                    json.dumps(criteria, ensure_ascii=False),
                    len(items),
                    success_count,
                    skipped_count,
                    error_count,
                    1 if dry_run else 0,
                    json.dumps(results, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return {
        "action_id": action_id,
        "preview_id": preview["preview_id"],
        "action": action,
        "dry_run": dry_run,
        "requested_count": len(items),
        "success_count": success_count,
        "skipped_count": skipped_count,
        "error_count": error_count,
        "remaining_count": max(0, int(data.get("total", 0)) - len(items)),
        "results": results[:200],
    }


def ad_control_rule_payload(row):
    item = dict(row)
    for key in ("criteria_json", "schedule_json", "thresholds_json", "last_result_json"):
        try:
            item[key.replace("_json", "")] = json.loads(item.get(key) or "{}")
        except Exception:
            item[key.replace("_json", "")] = {}
        item.pop(key, None)
    item["enabled"] = bool(item.get("enabled"))
    return item


def list_ad_control_rules():
    ensure_ad_control_tables()
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            rows = conn.execute("SELECT * FROM ad_control_rule ORDER BY updated_at DESC").fetchall()
            return {"items": [ad_control_rule_payload(row) for row in rows]}
        finally:
            conn.close()


def save_ad_control_rule(payload, session):
    ensure_ad_control_tables()
    criteria = ad_control_criteria(payload.get("criteria") or payload, require_action=True)
    rule_id = str(payload.get("rule_id") or "").strip() or uuid.uuid4().hex
    name = str(payload.get("name") or criteria["product"] + " " + criteria["action"]).strip()
    schedule = payload.get("schedule") or {}
    thresholds = payload.get("thresholds") or {}
    enabled = 1 if payload.get("enabled") else 0
    actor_user_id = str((session or {}).get("user_id") or "")
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO ad_control_rule (
                  rule_id, name, enabled, product, action, level, criteria_json,
                  schedule_json, thresholds_json, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(rule_id) DO UPDATE SET
                  name=excluded.name,
                  enabled=excluded.enabled,
                  product=excluded.product,
                  action=excluded.action,
                  level=excluded.level,
                  criteria_json=excluded.criteria_json,
                  schedule_json=excluded.schedule_json,
                  thresholds_json=excluded.thresholds_json,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    rule_id,
                    name,
                    enabled,
                    criteria["product"],
                    criteria["action"],
                    criteria["level"],
                    json.dumps(criteria, ensure_ascii=False),
                    json.dumps(schedule, ensure_ascii=False),
                    json.dumps(thresholds, ensure_ascii=False),
                    actor_user_id,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM ad_control_rule WHERE rule_id = ?", (rule_id,)).fetchone()
            return ad_control_rule_payload(row)
        finally:
            conn.close()


def set_ad_control_rule_enabled(rule_id, enabled):
    ensure_ad_control_tables()
    rule_id = str(rule_id or "").strip()
    if not rule_id:
        raise StructuredApiError("missing_rule_id", "缺少规则 ID")
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                "UPDATE ad_control_rule SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE rule_id=?",
                (1 if enabled else 0, rule_id),
            )
            if conn.total_changes < 1:
                raise StructuredApiError("rule_not_found", "规则不存在")
            conn.commit()
            row = conn.execute("SELECT * FROM ad_control_rule WHERE rule_id = ?", (rule_id,)).fetchone()
            return ad_control_rule_payload(row)
        finally:
            conn.close()


def ad_control_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def ad_control_json_loads(value, default):
    try:
        if value is None or value == "":
            return default
        return json.loads(value)
    except Exception:
        return default


def ad_control_json_dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def ad_control_actor(session):
    return str((session or {}).get("user_id") or "")


def ad_control_rule_hash(payload):
    return hashlib.sha256(ad_control_json_dumps(payload).encode("utf-8")).hexdigest()


def ad_control_account_key(account_id):
    normalized = ad_control_normalize_account(account_id)
    return "act_%s" % normalized if normalized else ""


def ad_control_parse_rule_group_path(path, suffix=""):
    prefix = "/api/ad-control/rule-groups/"
    if not path.startswith(prefix):
        return ""
    value = path[len(prefix):]
    if suffix:
        if not value.endswith(suffix):
            return ""
        value = value[:-len(suffix)]
    return value.strip("/")


def ad_control_parse_rule_set_path(path):
    prefix = "/api/ad-control/rule-sets/"
    if not path.startswith(prefix):
        return ""
    return path[len(prefix):].strip("/")


def ad_control_parse_binding_path(path, suffix=""):
    prefix = "/api/ad-control/bindings/"
    if not path.startswith(prefix):
        return ""
    value = path[len(prefix):]
    if suffix:
        if not value.endswith(suffix):
            return ""
        value = value[:-len(suffix)]
    return value.strip("/")


def ad_control_parse_account_group_path(path):
    prefix = "/api/ad-control/account-groups/"
    if not path.startswith(prefix):
        return ""
    return path[len(prefix):].strip("/")


def ad_control_safe_json_list(value):
    data = ad_control_json_loads(value, [])
    return data if isinstance(data, list) else []


def ad_control_safe_json_dict(value):
    data = ad_control_json_loads(value, {})
    return data if isinstance(data, dict) else {}


def ad_control_token_config_payload(row):
    item = dict(row)
    item["validation"] = ad_control_safe_json_dict(item.pop("validation_json", "{}"))
    item["account_id"] = item.get("account_id") or ""
    item["scope"] = "account" if item["account_id"] else "product"
    return item


def list_ad_control_token_config(product):
    product = str(product or "").strip()
    if not product:
        raise StructuredApiError("missing_product", "missing product")
    ensure_ad_control_tables()
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT * FROM ad_control_token_config
                 WHERE product = ?
                 ORDER BY CASE WHEN account_id='' THEN 0 ELSE 1 END, account_id
                """,
                (product,),
            ).fetchall()
            return {"items": [ad_control_token_config_payload(row) for row in rows]}
        finally:
            conn.close()


def save_ad_control_token_config(payload, session):
    product = str(payload.get("product") or "").strip()
    if not product:
        raise StructuredApiError("missing_product", "missing product")
    account_id = ad_control_normalize_account(payload.get("account_id"))
    user_id = str(payload.get("user_id") or "").strip()
    if not user_id:
        raise StructuredApiError("missing_user_id", "missing token owner user_id")
    label = str(payload.get("label") or "").strip()
    validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}
    token = ad_control_token_for_user_id(user_id)
    if not token:
        raise StructuredApiError("missing_meta_token", "token owner has no Meta token")
    if account_id:
        try:
            meta = ad_control_graph_get(token, ad_control_account_key(account_id), "id,account_id,name,account_status")
            validation = {
                "ok": True,
                "checked_count": 1,
                "ok_count": 1,
                "results": [{"account_id": account_id, "ok": True, "name": meta.get("name", "")}],
                "validated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception as exc:
            raise StructuredApiError("token_access_failed", "token cannot access selected account", account_id=account_id, reason=str(exc))
    elif not validation:
        validation = {
            "ok": True,
            "checked_count": 0,
            "ok_count": 0,
            "results": [],
            "validated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }
    ensure_ad_control_tables()
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO ad_control_token_config (
                  product, account_id, user_id, label, validation_json, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(product, account_id) DO UPDATE SET
                  user_id=excluded.user_id,
                  label=excluded.label,
                  validation_json=excluded.validation_json,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    product,
                    account_id,
                    user_id,
                    label,
                    json.dumps(validation, ensure_ascii=False),
                    ad_control_actor(session),
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM ad_control_token_config WHERE product=? AND account_id=?",
                (product, account_id),
            ).fetchone()
            return ad_control_token_config_payload(row)
        finally:
            conn.close()


def ad_control_token_for_user_id(user_id):
    user_id = str(user_id or "").strip()
    if not user_id:
        return ""
    sql = """
        SELECT accessToken
          FROM {table}
         WHERE CAST(user_id AS CHAR)={user_id}
           AND accessToken IS NOT NULL
           AND accessToken<>''
         LIMIT 1
    """.format(
        table=ad_control_table("ads_facebook_info"),
        user_id=ad_control_quote(user_id),
    )
    rows = run_mysql(" ".join(sql.split()))
    return str(rows[0][0] or "").strip() if rows else ""


def ad_control_token_config_for_accounts(product, account_ids):
    product = str(product or "").strip()
    accounts = [ad_control_normalize_account(item) for item in account_ids if ad_control_normalize_account(item)]
    ensure_ad_control_tables()
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM ad_control_token_config WHERE product=?",
                (product,),
            ).fetchall()
        finally:
            conn.close()
    by_account = {}
    default_config = None
    for row in rows:
        item = ad_control_token_config_payload(row)
        if item.get("account_id"):
            by_account[ad_control_normalize_account(item["account_id"])] = item
        else:
            default_config = item
    out = {}
    for account_id in accounts:
        out[account_id] = by_account.get(account_id) or default_config or {}
    return out


def validate_ad_control_token_config(payload):
    product = str(payload.get("product") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    accounts = [ad_control_normalize_account(item) for item in ad_control_list(payload.get("accounts") or payload.get("account_ids"))]
    if not product:
        raise StructuredApiError("missing_product", "missing product")
    if not user_id:
        raise StructuredApiError("missing_user_id", "missing token owner user_id")
    token = ad_control_token_for_user_id(user_id)
    if not token:
        raise StructuredApiError("missing_meta_token", "token owner has no Meta token")
    results = []
    ok_count = 0
    for account_id in accounts[:50]:
        try:
            meta = ad_control_graph_get(token, ad_control_account_key(account_id), "id,account_id,name,account_status")
            results.append({"account_id": account_id, "ok": True, "name": meta.get("name", "")})
            ok_count += 1
        except Exception as exc:
            results.append({"account_id": account_id, "ok": False, "reason": str(exc)})
    return {
        "product": product,
        "user_id": user_id,
        "ok": ok_count == len(accounts),
        "checked_count": len(accounts),
        "ok_count": ok_count,
        "results": results,
        "validated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    }


def ad_control_validate_scope_token_access(scope):
    token_configs = ad_control_token_config_for_accounts(scope["product"], scope["account_ids"])
    errors = []
    for account_id in scope["account_ids"]:
        token_user_id = str((token_configs.get(account_id) or {}).get("user_id") or "").strip()
        if not token_user_id:
            errors.append({"account_id": account_id, "reason": "missing_token_config"})
            continue
        token = ad_control_token_for_user_id(token_user_id)
        if not token:
            errors.append({"account_id": account_id, "token_user_id": token_user_id, "reason": "missing_meta_token"})
            continue
        try:
            ad_control_graph_get(token, ad_control_account_key(account_id), "id,account_id,name,account_status")
        except Exception as exc:
            errors.append({"account_id": account_id, "token_user_id": token_user_id, "reason": str(exc)})
    if errors:
        raise StructuredApiError(
            "token_access_failed",
            "token cannot access selected accounts",
            accounts=",".join(str(item.get("account_id") or "") for item in errors[:10]),
            errors=errors[:10],
        )
    return {"ok": True, "checked_count": len(scope["account_ids"])}


def ad_control_account_group_payload(row):
    item = dict(row)
    item["account_ids"] = ad_control_safe_json_list(item.pop("account_ids_json", "[]"))
    item["deleted"] = bool(item.get("deleted"))
    return item


def list_ad_control_account_groups(product=None):
    ensure_ad_control_tables()
    where = "WHERE deleted=0"
    params = []
    if product:
        where += " AND product=?"
        params.append(str(product or "").strip())
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM ad_control_account_group %s ORDER BY updated_at DESC" % where,
                params,
            ).fetchall()
            return {"items": [ad_control_account_group_payload(row) for row in rows]}
        finally:
            conn.close()


def save_ad_control_account_group(payload, session):
    product = str(payload.get("product") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not product:
        raise StructuredApiError("missing_product", "missing product")
    if not name:
        raise StructuredApiError("missing_name", "missing group name")
    group_id = str(payload.get("group_id") or "").strip() or uuid.uuid4().hex
    account_ids = [ad_control_normalize_account(item) for item in ad_control_list(payload.get("account_ids") or payload.get("accounts"))]
    account_ids = [item for item in account_ids if item]
    ensure_ad_control_tables()
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO ad_control_account_group (
                  group_id, name, product, account_ids_json, created_by, created_at, updated_at, deleted
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)
                ON CONFLICT(group_id) DO UPDATE SET
                  name=excluded.name,
                  product=excluded.product,
                  account_ids_json=excluded.account_ids_json,
                  updated_at=CURRENT_TIMESTAMP,
                  deleted=0
                """,
                (group_id, name, product, json.dumps(account_ids, ensure_ascii=False), ad_control_actor(session)),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM ad_control_account_group WHERE group_id=?", (group_id,)).fetchone()
            return ad_control_account_group_payload(row)
        finally:
            conn.close()


def delete_ad_control_account_group(group_id):
    ensure_ad_control_tables()
    group_id = str(group_id or "").strip()
    if not group_id:
        raise StructuredApiError("missing_group_id", "missing account group id")
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                "UPDATE ad_control_account_group SET deleted=1, updated_at=CURRENT_TIMESTAMP WHERE group_id=?",
                (group_id,),
            )
            if conn.total_changes < 1:
                raise StructuredApiError("not_found", "account group not found")
            conn.commit()
            return {"message": "deleted", "group_id": group_id}
        finally:
            conn.close()


def ad_control_rule_set_payload(row):
    item = dict(row)
    item["rules"] = ad_control_safe_json_list(item.pop("rules_json", "[]"))
    item["default_window"] = ad_control_safe_json_dict(item.pop("default_window_json", '{"type":"since_start"}'))
    item["deleted"] = bool(item.get("deleted"))
    return item


def list_ad_control_rule_sets(product=None):
    ensure_ad_control_tables()
    where = "WHERE deleted=0"
    params = []
    if product:
        where += " AND product=?"
        params.append(str(product or "").strip())
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM ad_control_rule_set %s ORDER BY updated_at DESC" % where,
                params,
            ).fetchall()
            return {"items": [ad_control_rule_set_payload(row) for row in rows]}
        finally:
            conn.close()


def fetch_ad_control_rule_set(rule_set_id):
    ensure_ad_control_tables()
    rule_set_id = str(rule_set_id or "").strip()
    if not rule_set_id:
        raise StructuredApiError("missing_rule_set_id", "missing rule set id")
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            row = conn.execute(
                "SELECT * FROM ad_control_rule_set WHERE rule_set_id=? AND deleted=0",
                (rule_set_id,),
            ).fetchone()
            if not row:
                raise StructuredApiError("not_found", "rule set not found")
            return ad_control_rule_set_payload(row)
        finally:
            conn.close()


def save_ad_control_rule_set(payload, session):
    product = str(payload.get("product") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not product:
        raise StructuredApiError("missing_product", "missing product")
    if not name:
        raise StructuredApiError("missing_name", "missing rule set name")
    rule_set_id = str(payload.get("rule_set_id") or "").strip() or uuid.uuid4().hex
    rules = payload.get("rules") if isinstance(payload.get("rules"), list) else []
    default_window = payload.get("default_window") if isinstance(payload.get("default_window"), dict) else {}
    if not default_window:
        default_window = payload.get("window") if isinstance(payload.get("window"), dict) else {"type": "since_start"}
    ensure_ad_control_tables()
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO ad_control_rule_set (
                  rule_set_id, name, product, rules_json, default_window_json,
                  created_by, created_at, updated_at, deleted
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)
                ON CONFLICT(rule_set_id) DO UPDATE SET
                  name=excluded.name,
                  product=excluded.product,
                  rules_json=excluded.rules_json,
                  default_window_json=excluded.default_window_json,
                  updated_at=CURRENT_TIMESTAMP,
                  deleted=0
                """,
                (
                    rule_set_id,
                    name,
                    product,
                    json.dumps(rules, ensure_ascii=False),
                    json.dumps(default_window, ensure_ascii=False),
                    ad_control_actor(session),
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM ad_control_rule_set WHERE rule_set_id=?", (rule_set_id,)).fetchone()
            return ad_control_rule_set_payload(row)
        finally:
            conn.close()


def delete_ad_control_rule_set(rule_set_id):
    ensure_ad_control_tables()
    rule_set_id = str(rule_set_id or "").strip()
    if not rule_set_id:
        raise StructuredApiError("missing_rule_set_id", "missing rule set id")
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            active_bindings = conn.execute(
                """
                SELECT COUNT(*)
                  FROM ad_control_rule_group
                 WHERE rule_set_id=? AND deleted=0
                """,
                (rule_set_id,),
            ).fetchone()[0]
            if active_bindings:
                raise StructuredApiError("rule_set_in_use", "rule set is used by bindings", binding_count=active_bindings)
            conn.execute(
                "UPDATE ad_control_rule_set SET deleted=1, updated_at=CURRENT_TIMESTAMP WHERE rule_set_id=?",
                (rule_set_id,),
            )
            if conn.total_changes < 1:
                raise StructuredApiError("not_found", "rule set not found")
            conn.commit()
            return {"message": "deleted", "rule_set_id": rule_set_id}
        finally:
            conn.close()


def ad_control_rule_group_payload(row):
    item = dict(row)
    item["account_ids"] = ad_control_safe_json_list(item.pop("account_ids_json", "[]"))
    item["rules"] = ad_control_safe_json_list(item.pop("rules_json", "[]"))
    item["strategy"] = ad_control_safe_json_dict(item.pop("strategy_json", "{}"))
    rule_set_rules = ad_control_safe_json_list(item.pop("rule_set_rules_json", "[]"))
    if not item["rules"] and rule_set_rules:
        item["rules"] = rule_set_rules
    item["rule_set_default_window"] = ad_control_safe_json_dict(item.pop("rule_set_default_window_json", "{}"))
    item["last_result"] = ad_control_safe_json_dict(item.pop("last_result_json", "{}"))
    item["enabled"] = bool(item.get("enabled"))
    item["emergency_stopped"] = bool(item.get("emergency_stopped"))
    item["deleted"] = bool(item.get("deleted"))
    item["binding_id"] = item.get("group_id", "")
    item["rule_set_id"] = item.get("rule_set_id", "")
    return item


def list_ad_control_rule_groups(product=None):
    ensure_ad_control_tables()
    where = "WHERE g.deleted=0"
    params = []
    if product:
        where += " AND g.product=?"
        params.append(str(product or "").strip())
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT g.*,
                       rs.name AS rule_set_name,
                       rs.rules_json AS rule_set_rules_json,
                       rs.default_window_json AS rule_set_default_window_json
                  FROM ad_control_rule_group g
             LEFT JOIN ad_control_rule_set rs
                    ON rs.rule_set_id = g.rule_set_id
                   AND rs.deleted = 0
                  %s
              ORDER BY g.updated_at DESC
                """ % where,
                params,
            ).fetchall()
            return {"items": [ad_control_rule_group_payload(row) for row in rows]}
        finally:
            conn.close()


def list_ad_control_bindings(product=None):
    return list_ad_control_rule_groups(product)


def fetch_ad_control_rule_group(group_id):
    ensure_ad_control_tables()
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            row = conn.execute(
                """
                SELECT g.*,
                       rs.name AS rule_set_name,
                       rs.rules_json AS rule_set_rules_json,
                       rs.default_window_json AS rule_set_default_window_json
                  FROM ad_control_rule_group g
             LEFT JOIN ad_control_rule_set rs
                    ON rs.rule_set_id = g.rule_set_id
                   AND rs.deleted = 0
                 WHERE g.group_id=? AND g.deleted=0
                """,
                (str(group_id or "").strip(),),
            ).fetchone()
            if not row:
                raise StructuredApiError("not_found", "rule group not found")
            return ad_control_rule_group_payload(row)
        finally:
            conn.close()


def fetch_ad_control_binding(binding_id):
    return fetch_ad_control_rule_group(binding_id)


def save_ad_control_rule_group(payload, session):
    product = str(payload.get("product") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not product:
        raise StructuredApiError("missing_product", "missing product")
    if not name:
        raise StructuredApiError("missing_name", "missing rule group name")
    group_id = str(payload.get("group_id") or "").strip() or uuid.uuid4().hex
    account_group_id = str(payload.get("account_group_id") or "").strip()
    account_ids = [ad_control_normalize_account(item) for item in ad_control_list(payload.get("account_ids") or payload.get("accounts"))]
    account_ids = [item for item in account_ids if item]
    rule_set_id = str(payload.get("rule_set_id") or "").strip()
    rules = payload.get("rules") if isinstance(payload.get("rules"), list) else []
    strategy = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {}
    if rule_set_id:
        rule_set = fetch_ad_control_rule_set(rule_set_id)
        if rule_set.get("product") != product:
            raise StructuredApiError("rule_set_product_mismatch", "rule set product does not match binding product")
        if not rules:
            rules = rule_set.get("rules") or []
    elif rules:
        rule_set_id = "legacy_%s" % group_id
        save_ad_control_rule_set(
            {
                "rule_set_id": rule_set_id,
                "product": product,
                "name": name,
                "rules": rules,
                "default_window": payload.get("default_window") if isinstance(payload.get("default_window"), dict) else {"type": "since_start"},
            },
            session,
        )
    enabled = 1 if payload.get("enabled") else 0
    if enabled:
        validate_account_ids = list(account_ids)
        if not validate_account_ids and account_group_id:
            groups = list_ad_control_account_groups(product).get("items", [])
            for account_group in groups:
                if account_group.get("group_id") == account_group_id:
                    validate_account_ids = [ad_control_normalize_account(item) for item in account_group.get("account_ids", [])]
                    break
        validate_account_ids = [item for item in validate_account_ids if item]
        if not validate_account_ids:
            raise StructuredApiError("missing_accounts", "select accounts before enabling rule group")
        ad_control_validate_scope_token_access({
            "product": product,
            "account_ids": validate_account_ids,
        })
    ensure_ad_control_tables()
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO ad_control_rule_group (
                  group_id, name, product, rule_set_id, account_group_id, account_ids_json, rules_json, strategy_json,
                  enabled, emergency_stopped, created_by, created_at, updated_at, deleted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)
                ON CONFLICT(group_id) DO UPDATE SET
                  name=excluded.name,
                  product=excluded.product,
                  rule_set_id=excluded.rule_set_id,
                  account_group_id=excluded.account_group_id,
                  account_ids_json=excluded.account_ids_json,
                  rules_json=excluded.rules_json,
                  strategy_json=excluded.strategy_json,
                  enabled=excluded.enabled,
                  updated_at=CURRENT_TIMESTAMP,
                  deleted=0
                """,
                (
                    group_id,
                    name,
                    product,
                    rule_set_id,
                    account_group_id,
                    json.dumps(account_ids, ensure_ascii=False),
                    json.dumps(rules, ensure_ascii=False),
                    json.dumps(strategy, ensure_ascii=False),
                    enabled,
                    ad_control_actor(session),
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM ad_control_rule_group WHERE group_id=?", (group_id,)).fetchone()
            return ad_control_rule_group_payload(row)
        finally:
            conn.close()


def save_ad_control_binding(payload, session):
    return save_ad_control_rule_group(payload, session)


def delete_ad_control_rule_group(group_id):
    ensure_ad_control_tables()
    group_id = str(group_id or "").strip()
    if not group_id:
        raise StructuredApiError("missing_group_id", "missing rule group id")
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                "UPDATE ad_control_rule_group SET deleted=1, enabled=0, updated_at=CURRENT_TIMESTAMP WHERE group_id=?",
                (group_id,),
            )
            if conn.total_changes < 1:
                raise StructuredApiError("not_found", "rule group not found")
            conn.commit()
            return {"message": "deleted", "group_id": group_id}
        finally:
            conn.close()


def delete_ad_control_binding(binding_id):
    return delete_ad_control_rule_group(binding_id)


def set_ad_control_rule_group_enabled(group_id, enabled):
    ensure_ad_control_tables()
    group = fetch_ad_control_rule_group(group_id)
    if enabled:
        if not group.get("last_preview_id") or not group.get("last_preview_hash"):
            raise StructuredApiError("preview_required", "preview this rule group before enabling it")
        scope = ad_control_resolve_live_scope({"rule_group_id": group_id})
        ad_control_validate_scope_token_access(scope)
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            if enabled:
                conn.execute(
                    """
                    UPDATE ad_control_rule_group
                       SET enabled=1,
                           emergency_stopped=0,
                           updated_at=CURRENT_TIMESTAMP
                     WHERE group_id=?
                    """,
                    (group_id,),
                )
            else:
                conn.execute(
                    """
                    UPDATE ad_control_rule_group
                       SET enabled=0,
                           updated_at=CURRENT_TIMESTAMP
                     WHERE group_id=?
                    """,
                    (group_id,),
                )
            conn.commit()
        finally:
            conn.close()
    return fetch_ad_control_rule_group(group_id)


def set_ad_control_binding_enabled(binding_id, enabled):
    return set_ad_control_rule_group_enabled(binding_id, enabled)


def ad_control_emergency_stop(payload):
    scope = str(payload.get("scope") or "global").strip()
    group_id = str(payload.get("group_id") or "").strip()
    ensure_ad_control_tables()
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            if scope == "rule_group" and group_id:
                conn.execute(
                    "UPDATE ad_control_rule_group SET emergency_stopped=1, enabled=0, updated_at=CURRENT_TIMESTAMP WHERE group_id=?",
                    (group_id,),
                )
            else:
                conn.execute(
                    "UPDATE ad_control_rule_group SET emergency_stopped=1, enabled=0, updated_at=CURRENT_TIMESTAMP WHERE deleted=0"
                )
            conn.commit()
        finally:
            conn.close()
    return {"message": "stopped", "scope": scope, "group_id": group_id}


def ad_control_runner_status():
    resource = ad_control_resource_snapshot()
    groups = list_ad_control_rule_groups().get("items", [])
    return {
        "resource": resource,
        "enabled_rule_groups": len([item for item in groups if item.get("enabled")]),
        "emergency_stopped_groups": len([item for item in groups if item.get("emergency_stopped")]),
        "max_workers": AD_CONTROL_LIVE_MAX_WORKERS,
        "resource_limit_percent": AD_CONTROL_RESOURCE_LIMIT_PERCENT,
    }


def list_ad_control_actions(limit=50, product="", binding_id="", action="", date_from="", date_to=""):
    ensure_ad_control_tables()
    limit = ad_control_int(limit, 50, 1, 200)
    product = str(product or "").strip()
    binding_id = str(binding_id or "").strip()
    action = str(action or "").strip()
    date_from = str(date_from or "").strip()
    date_to = str(date_to or "").strip()
    where = []
    params = []
    if product:
        where.append("product=?")
        params.append(product)
    if action:
        where.append("action=?")
        params.append(action)
    if date_from:
        where.append("created_at>=?")
        params.append(date_from + " 00:00:00")
    if date_to:
        where.append("created_at<=?")
        params.append(date_to + " 23:59:59")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    query_limit = limit if not binding_id else min(1000, max(limit * 5, 200))
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM ad_control_action %s ORDER BY created_at DESC LIMIT ?" % where_sql,
                tuple(params + [query_limit]),
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["criteria"] = ad_control_safe_json_dict(item.pop("criteria_json", "{}"))
                item["results"] = ad_control_safe_json_list(item.pop("results_json", "[]"))
                item["dry_run"] = bool(item.get("dry_run"))
                item["binding_id"] = item["criteria"].get("binding_id") or item["criteria"].get("rule_group_id") or ""
                if binding_id and item["binding_id"] != binding_id:
                    continue
                items.append(item)
                if len(items) >= limit:
                    break
            return {"items": items}
        finally:
            conn.close()


def ad_control_resource_snapshot():
    cpu_percent = None
    mem_percent = None
    try:
        if hasattr(os, "getloadavg"):
            load1 = os.getloadavg()[0]
            cpu_count = os.cpu_count() or 1
            cpu_percent = min(100.0, max(0.0, (load1 / cpu_count) * 100.0))
    except Exception:
        cpu_percent = None
    try:
        meminfo = {}
        with open("/proc/meminfo", "r") as handle:
            for line in handle:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    meminfo[parts[0]] = ad_control_float(parts[1].strip().split()[0], 0.0)
        total = meminfo.get("MemTotal", 0.0)
        available = meminfo.get("MemAvailable", 0.0)
        if total > 0:
            mem_percent = max(0.0, min(100.0, ((total - available) / total) * 100.0))
    except Exception:
        mem_percent = None
    over_limit = any(
        value is not None and value >= AD_CONTROL_RESOURCE_LIMIT_PERCENT
        for value in (cpu_percent, mem_percent)
    )
    return {"cpu_percent": cpu_percent, "memory_percent": mem_percent, "over_limit": over_limit}


def ad_control_redis_parse_url():
    if not AD_CONTROL_REDIS_URL:
        return None
    parsed = urlparse(AD_CONTROL_REDIS_URL)
    if parsed.scheme not in ("redis", "rediss"):
        return None
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 6379,
        "password": parsed.password or "",
        "db": int((parsed.path or "/0").strip("/") or "0"),
        "ssl": parsed.scheme == "rediss",
    }


def ad_control_redis_command(*parts):
    config = ad_control_redis_parse_url()
    if not config:
        return None
    payload = ("*%d\r\n" % len(parts)).encode("utf-8")
    for part in parts:
        raw = str(part).encode("utf-8")
        payload += ("$%d\r\n" % len(raw)).encode("utf-8") + raw + b"\r\n"
    sock = socket.create_connection((config["host"], config["port"]), timeout=1.5)
    try:
        if config["password"]:
            ad_control_redis_send(sock, "AUTH", config["password"])
        if config["db"]:
            ad_control_redis_send(sock, "SELECT", str(config["db"]))
        sock.sendall(payload)
        return ad_control_redis_read(sock)
    finally:
        sock.close()


def ad_control_redis_send(sock, *parts):
    payload = ("*%d\r\n" % len(parts)).encode("utf-8")
    for part in parts:
        raw = str(part).encode("utf-8")
        payload += ("$%d\r\n" % len(raw)).encode("utf-8") + raw + b"\r\n"
    sock.sendall(payload)
    return ad_control_redis_read(sock)


def ad_control_redis_read(sock):
    prefix = sock.recv(1)
    if not prefix:
        return None
    line = b""
    while not line.endswith(b"\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            break
        line += chunk
    text = line[:-2].decode("utf-8", "replace")
    if prefix == b"+":
        return text
    if prefix == b"-":
        raise RuntimeError(text)
    if prefix == b":":
        return int(text or "0")
    if prefix == b"$":
        size = int(text or "-1")
        if size < 0:
            return None
        data = b""
        while len(data) < size + 2:
            data += sock.recv(size + 2 - len(data))
        return data[:size].decode("utf-8", "replace")
    return text


def ad_control_campaign_start_key(product, account_id, campaign_id):
    return "ad_control:campaign_start:%s:%s:%s" % (
        str(product or "").strip(),
        ad_control_normalize_account(account_id),
        str(campaign_id or "").strip(),
    )


def ad_control_get_cached_campaign_start(product, account_id, campaign_id):
    key = ad_control_campaign_start_key(product, account_id, campaign_id)
    try:
        raw = ad_control_redis_command("GET", key)
        data = ad_control_safe_json_dict(raw)
        if data.get("campaign_start_at"):
            data["cache"] = "redis"
            return data
    except Exception as exc:
        logging.info("ad control redis get failed: %s", exc)
    return {}


def ad_control_set_cached_campaign_start(product, account_id, campaign_id, value):
    key = ad_control_campaign_start_key(product, account_id, campaign_id)
    payload = {
        "campaign_start_at": value.get("campaign_start_at", ""),
        "source_table": value.get("source_table", ""),
        "source_field": value.get("source_field", ""),
        "cached_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        ad_control_redis_command("SET", key, json.dumps(payload, ensure_ascii=False))
        payload["cache"] = "redis"
    except Exception as exc:
        logging.info("ad control redis set failed: %s", exc)
        payload["cache"] = "none"
    return payload


def ad_control_delete_cached_campaign_start(product, account_id, campaign_id):
    key = ad_control_campaign_start_key(product, account_id, campaign_id)
    try:
        ad_control_redis_command("DEL", key)
    except Exception as exc:
        logging.info("ad control redis del failed: %s", exc)
    return {"message": "refreshed", "key": key}


def ad_control_validate_insight_start_schema():
    columns = mysql_table_columns(AD_CONTROL_INSIGHT_START_TABLE, AD_CONTROL_DB_NAME)
    required = [AD_CONTROL_INSIGHT_CAMPAIGN_FIELD, AD_CONTROL_INSIGHT_START_FIELD]
    missing = [item for item in required if item not in columns]
    if missing:
        raise StructuredApiError(
            "invalid_insight_start_schema",
            "insight start table missing required fields",
            table=AD_CONTROL_INSIGHT_START_TABLE,
            missing=",".join(missing),
        )
    return columns


def ad_control_query_campaign_starts(product, account_id, campaign_ids):
    campaign_ids = [str(item or "").strip() for item in campaign_ids if str(item or "").strip()]
    if not campaign_ids:
        return {}
    columns = ad_control_validate_insight_start_schema()
    where = [
        "%s IN %s" % (sql_identifier(AD_CONTROL_INSIGHT_CAMPAIGN_FIELD), ad_control_sql_in(campaign_ids)),
        "%s IS NOT NULL" % sql_identifier(AD_CONTROL_INSIGHT_START_FIELD),
    ]
    if AD_CONTROL_INSIGHT_ACCOUNT_FIELD and AD_CONTROL_INSIGHT_ACCOUNT_FIELD in columns:
        where.append("%s=%s" % (
            ad_control_norm_account_sql(sql_identifier(AD_CONTROL_INSIGHT_ACCOUNT_FIELD)),
            ad_control_quote(ad_control_normalize_account(account_id)),
        ))
    if AD_CONTROL_INSIGHT_PRODUCT_FIELD and AD_CONTROL_INSIGHT_PRODUCT_FIELD in columns:
        where.append("%s=%s" % (sql_identifier(AD_CONTROL_INSIGHT_PRODUCT_FIELD), ad_control_quote(product)))
    sql = """
        SELECT CAST({campaign_field} AS CHAR), MIN({start_field})
          FROM {table}
         WHERE {where_sql}
         GROUP BY CAST({campaign_field} AS CHAR)
    """.format(
        campaign_field=sql_identifier(AD_CONTROL_INSIGHT_CAMPAIGN_FIELD),
        start_field=sql_identifier(AD_CONTROL_INSIGHT_START_FIELD),
        table=ad_control_table(AD_CONTROL_INSIGHT_START_TABLE),
        where_sql=" AND ".join(where),
    )
    rows = run_mysql(" ".join(sql.split()))
    out = {}
    for row in rows:
        campaign_id = str(row[0] or "").strip()
        start_at = str(row[1] or "").strip()
        if campaign_id and start_at:
            out[campaign_id] = {
                "campaign_start_at": start_at,
                "source_table": AD_CONTROL_INSIGHT_START_TABLE,
                "source_field": AD_CONTROL_INSIGHT_START_FIELD,
            }
    return out


def ad_control_campaign_start(product, account_id, campaign_id, refresh=False):
    if not refresh:
        cached = ad_control_get_cached_campaign_start(product, account_id, campaign_id)
        if cached:
            return cached
    data = ad_control_query_campaign_starts(product, account_id, [campaign_id]).get(str(campaign_id), {})
    if data.get("campaign_start_at"):
        return ad_control_set_cached_campaign_start(product, account_id, campaign_id, data)
    return {"campaign_start_at": "", "cache": "miss", "reason": "missing_campaign_start_at"}


def ad_control_parse_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:26] if "%f" in fmt else text[:19 if "H" in fmt else 10], fmt)
        except Exception:
            pass
    return None


def ad_control_age_hours(start_at):
    dt = ad_control_parse_datetime(start_at)
    if not dt:
        return None
    return max(0.0, (datetime.utcnow() - dt).total_seconds() / 3600.0)


def ad_control_product_campaign_whitelist(product, account_ids):
    accounts = [ad_control_normalize_account(item) for item in account_ids if ad_control_normalize_account(item)]
    if not product or not accounts:
        return {}
    sql = """
        SELECT
          {account_norm},
          CAST(d.campaign_id AS CHAR),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(NULLIF(d.campaign_name,'') ORDER BY d.updated_at DESC SEPARATOR '\\n'), '\\n', 1), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(NULLIF(d.country,'') ORDER BY d.updated_at DESC SEPARATOR ','), ',', 1), ''),
          COALESCE(SUBSTRING_INDEX(GROUP_CONCAT(NULLIF(d.language,'') ORDER BY d.updated_at DESC SEPARATOR ','), ',', 1), ''),
          COALESCE(MAX(CAST(s.time_zone AS CHAR)), '')
          FROM {table} d
     LEFT JOIN {accounts_table} s
            ON s.platform_id=1
           AND {account_norm}= {setting_norm}
         WHERE d.product={product}
           AND d.campaign_id IS NOT NULL
           AND d.campaign_id<>''
           AND {account_norm} IN {accounts}
         GROUP BY {account_norm}, CAST(d.campaign_id AS CHAR)
    """.format(
        account_norm=ad_control_norm_account_sql("d.ad_account_id"),
        setting_norm=ad_control_norm_account_sql("s.account_id"),
        table=ad_control_table("ads_facebook_auto_created_data"),
        accounts_table=ad_control_table("ads_accounts_setting"),
        product=ad_control_quote(product),
        accounts=ad_control_sql_in(accounts),
    )
    rows = run_mysql(" ".join(sql.split()))
    out = {}
    for row in rows:
        account_id = ad_control_normalize_account(row[0])
        campaign_id = str(row[1] or "").strip()
        if account_id and campaign_id:
            out.setdefault(account_id, {})[campaign_id] = {
                "campaign_name": str(row[2] or ""),
                "country": str(row[3] or "").strip(),
                "language": str(row[4] or "").strip(),
                "account_time_zone": str(row[5] or "").strip(),
            }
    return out


def ad_control_graph_paged_get(token, object_id, edge, params):
    url = "https://graph.facebook.com/%s/%s/%s" % (AD_CONTROL_GRAPH_VERSION, object_id, edge)
    params = dict(params or {})
    params["access_token"] = token
    items = []
    while url:
        response = requests.get(url, params=params, timeout=AD_CONTROL_GRAPH_TIMEOUT)
        payload = response.json() if response.content else {}
        if response.status_code >= 400 or payload.get("error"):
            raise RuntimeError(json.dumps(payload.get("error") or payload, ensure_ascii=False))
        items.extend(payload.get("data") or [])
        next_url = ((payload.get("paging") or {}).get("next") or "").strip()
        url = next_url or ""
        params = {}
        if len(items) >= AD_CONTROL_MAX_LIVE_CAMPAIGNS:
            break
    return items


def ad_control_meta_active_campaigns(token, account_id):
    params = {
        "fields": "id,name,status,effective_status",
        "limit": "500",
        "effective_status": json.dumps(["ACTIVE"]),
    }
    return ad_control_graph_paged_get(token, ad_control_account_key(account_id), "campaigns", params)


def ad_control_metric_window(rule, default_window, start_at):
    window = rule.get("window") if isinstance(rule, dict) else {}
    if not isinstance(window, dict):
        window = {}
    if not window:
        window = default_window if isinstance(default_window, dict) else {}
    window_type = str(window.get("type") or "since_start").strip()
    today = datetime.utcnow().date()
    until = today.strftime("%Y-%m-%d")
    if window_type == "today":
        since = until
    elif window_type == "recent_hours":
        hours = ad_control_int(window.get("hours", 24), 24, 1, 720)
        since = (datetime.utcnow() - timedelta(hours=hours)).date().strftime("%Y-%m-%d")
    else:
        start_dt = ad_control_parse_datetime(start_at)
        since = start_dt.date().strftime("%Y-%m-%d") if start_dt else until
        window_type = "since_start"
    return {"type": window_type, "since": since, "until": until}


def ad_control_extract_action(actions, names):
    total = 0.0
    for action in actions or []:
        action_type = str(action.get("action_type") or "")
        if action_type in names:
            total += ad_control_float(action.get("value"), 0.0)
    return total


def ad_control_parse_insight_row(row):
    spend = ad_control_float(row.get("spend"), 0.0)
    install = ad_control_extract_action(row.get("actions"), {"mobile_app_install", "omni_app_install", "app_install"})
    purchase = ad_control_extract_action(row.get("actions"), {"purchase", "omni_purchase", "offsite_conversion.fb_pixel_purchase"})
    revenue = ad_control_extract_action(row.get("action_values"), {"purchase", "omni_purchase", "offsite_conversion.fb_pixel_purchase"})
    roas = 0.0
    purchase_roas = row.get("purchase_roas") or []
    if purchase_roas:
        roas = ad_control_float(purchase_roas[0].get("value"), 0.0)
    if not roas and spend > 0 and revenue:
        roas = revenue / spend
    return {
        "spend": spend,
        "install": int(install),
        "purchase": int(purchase),
        "revenue": revenue,
        "roas": roas,
        "roas_pct": roas * 100.0,
        "purchase_cpa": (spend / purchase) if purchase else None,
    }


def ad_control_merge_metrics(metrics):
    out = {"spend": 0.0, "install": 0, "purchase": 0, "revenue": 0.0, "roas": 0.0, "roas_pct": 0.0, "purchase_cpa": None}
    for item in metrics:
        out["spend"] += ad_control_float(item.get("spend"), 0.0)
        out["install"] += int(ad_control_float(item.get("install"), 0.0))
        out["purchase"] += int(ad_control_float(item.get("purchase"), 0.0))
        out["revenue"] += ad_control_float(item.get("revenue"), 0.0)
    if out["spend"] > 0:
        out["roas"] = out["revenue"] / out["spend"]
        out["roas_pct"] = out["roas"] * 100.0
    if out["purchase"] > 0:
        out["purchase_cpa"] = out["spend"] / out["purchase"]
    return out


def ad_control_meta_account_insights(token, account_id, campaign_ids, since, until):
    out = {}
    fields = "campaign_id,campaign_name,spend,actions,action_values,purchase_roas"
    ids = [str(item or "").strip() for item in campaign_ids if str(item or "").strip()]
    for offset in range(0, len(ids), 50):
        chunk = ids[offset:offset + 50]
        filtering = [{"field": "campaign.id", "operator": "IN", "value": chunk}]
        params = {
            "level": "campaign",
            "fields": fields,
            "time_range": json.dumps({"since": since, "until": until}),
            "filtering": json.dumps(filtering),
            "limit": "500",
        }
        rows = ad_control_graph_paged_get(token, ad_control_account_key(account_id), "insights", params)
        for row in rows:
            campaign_id = str(row.get("campaign_id") or "").strip()
            if campaign_id:
                out[campaign_id] = ad_control_parse_insight_row(row)
    return out


def ad_control_condition_value(item, field):
    metrics = item.get("metrics") or {}
    field = str(field or "").strip()
    if field.startswith("metrics."):
        field = field.split(".", 1)[1]
    aliases = {
        "spend_usd": "spend",
        "installs": "install",
        "purchases": "purchase",
        "roas": "roas_pct",
        "purchase_cpa_usd": "purchase_cpa",
        "cpa": "purchase_cpa",
        "country_group": "country",
        "geo": "country",
        "region": "country",
        "lang": "language",
        "locale": "language",
        "time_zone": "account_time_zone",
        "timezone": "account_time_zone",
        "account_timezone": "account_time_zone",
    }
    field = aliases.get(field, field)
    if field in ("age_hours", "runtime_hours"):
        return item.get("age_hours")
    if field in ("status", "effective_status"):
        return item.get(field) or item.get("effective_status") or item.get("status")
    if field in metrics:
        return metrics.get(field)
    return item.get(field)


def ad_control_timezone_values(value):
    text = str(value or "").strip()
    if not text:
        return set()
    values = {text, text.upper()}
    match = re.search(r"([+-]?\d{1,2})(?:[:.]?(\d{1,2}))?$", text)
    if not match:
        return values
    try:
        hours = int(match.group(1))
        minutes = int((match.group(2) or "0")[:2])
    except Exception:
        return values
    if minutes == 0:
        values.update({
            str(hours),
            "%+d" % hours,
            "UTC%+d" % hours,
            "UTC%+03d:00" % hours,
            "GMT%+d" % hours,
            "GMT%+03d:00" % hours,
        })
    return {item.upper() for item in values if item}


def ad_control_string_values(value):
    return {str(value or "").strip(), str(value or "").strip().upper()}


def ad_control_match_condition(item, condition):
    field = condition.get("field")
    field_key = str(field or "").strip()
    op = str(condition.get("op") or condition.get("operator") or "eq").lower()
    actual = ad_control_condition_value(item, field)
    expected = condition.get("value")
    if op in ("exists", "present"):
        return actual is not None and actual != ""
    if actual is None:
        return False
    if op in ("in", "not_in"):
        values = expected if isinstance(expected, list) else ad_control_list(expected)
        if field_key in ("account_time_zone", "time_zone", "timezone", "account_timezone"):
            expected_values = set()
            for value in values:
                expected_values.update(ad_control_timezone_values(value))
            matched = bool(ad_control_timezone_values(actual) & expected_values)
        elif field_key in ("country", "country_group", "geo", "region"):
            matched = str(actual or "").strip().upper() in [str(value or "").strip().upper() for value in values]
        elif field_key in ("language", "lang", "locale"):
            matched = str(actual or "").strip().upper() in [str(value or "").strip().upper() for value in values]
        else:
            matched = str(actual) in [str(value) for value in values]
        return not matched if op == "not_in" else matched
    if op == "between":
        values = expected if isinstance(expected, list) else [condition.get("min"), condition.get("max")]
        if len(values) < 2:
            return False
        number = ad_control_float(actual, None)
        if number is None:
            return False
        return number >= ad_control_float(values[0]) and number <= ad_control_float(values[1])
    if op in ("gt", "gte", "lt", "lte"):
        number = ad_control_float(actual, None)
        target = ad_control_float(expected, None)
        if number is None or target is None:
            return False
        if op == "gt":
            return number > target
        if op == "gte":
            return number >= target
        if op == "lt":
            return number < target
        return number <= target
    if op in ("ne", "neq"):
        if field_key in ("account_time_zone", "time_zone", "timezone", "account_timezone"):
            return not bool(ad_control_timezone_values(actual) & ad_control_timezone_values(expected))
        if field_key in ("country", "country_group", "geo", "region"):
            return str(actual or "").strip().upper() != str(expected or "").strip().upper()
        if field_key in ("language", "lang", "locale"):
            return str(actual or "").strip().upper() != str(expected or "").strip().upper()
        return str(actual) != str(expected)
    if field_key in ("account_time_zone", "time_zone", "timezone", "account_timezone"):
        return bool(ad_control_timezone_values(actual) & ad_control_timezone_values(expected))
    if field_key in ("country", "country_group", "geo", "region"):
        return str(actual or "").strip().upper() == str(expected or "").strip().upper()
    if field_key in ("language", "lang", "locale"):
        return str(actual or "").strip().upper() == str(expected or "").strip().upper()
    return str(actual) == str(expected)


def ad_control_evaluate_rules(item, rules):
    matched = []
    target_action = "observe"
    for rule in rules or []:
        if not isinstance(rule, dict) or rule.get("enabled") is False:
            continue
        conditions = rule.get("conditions") if isinstance(rule.get("conditions"), list) else []
        if conditions and not all(ad_control_match_condition(item, condition) for condition in conditions):
            continue
        action = str(rule.get("action") or "observe").lower()
        if action in ("pause", "close", "stop"):
            action = "pause"
            target_action = "pause"
        else:
            action = "observe"
        matched.append({"name": rule.get("name") or "", "action": action})
    return {"matched_rules": matched, "target_action": target_action if matched else "none"}


def ad_control_resolve_live_scope(payload):
    group = None
    if payload.get("rule_group_id"):
        group = fetch_ad_control_rule_group(payload.get("rule_group_id"))
        product = group["product"]
        rules = group.get("rules") or []
        default_window = group.get("rule_set_default_window") or {"type": "since_start"}
        account_group_id = group.get("account_group_id") or ""
        account_ids = list(group.get("account_ids") or [])
        if account_group_id:
            account_group = list_ad_control_account_groups(product).get("items", [])
            match = [item for item in account_group if item.get("group_id") == account_group_id]
            if match:
                account_ids = list(match[0].get("account_ids") or [])
    else:
        product = str(payload.get("product") or "").strip()
        rules = payload.get("rules") if isinstance(payload.get("rules"), list) else []
        default_window = {"type": "since_start"}
        account_ids = [ad_control_normalize_account(item) for item in ad_control_list(payload.get("account_ids") or payload.get("accounts"))]
    if not product:
        raise StructuredApiError("missing_product", "missing product")
    account_ids = [ad_control_normalize_account(item) for item in account_ids if ad_control_normalize_account(item)]
    if not account_ids:
        raise StructuredApiError("missing_accounts", "select at least one account")
    if len(account_ids) > AD_CONTROL_MAX_LIVE_ACCOUNTS:
        raise StructuredApiError("too_many_accounts", "too many accounts", max_accounts=AD_CONTROL_MAX_LIVE_ACCOUNTS)
    if not rules:
        rules = [{"name": "observe all", "action": "observe", "enabled": True, "conditions": []}]
    strategy = (group or {}).get("strategy") if isinstance((group or {}).get("strategy"), dict) else {}
    if not strategy and isinstance(payload.get("strategy"), dict):
        strategy = payload.get("strategy") or {}
    return {
        "product": product,
        "account_ids": account_ids,
        "rules": rules,
        "rule_group": group,
        "rule_group_id": (group or {}).get("group_id") or str(payload.get("rule_group_id") or ""),
        "strategy": strategy,
        "window": payload.get("window") if isinstance(payload.get("window"), dict) else default_window,
    }


def ad_control_collect_live_account(scope, account_id, token_config, whitelist):
    account_id = ad_control_normalize_account(account_id)
    token_user_id = str((token_config or {}).get("user_id") or "").strip()
    token = ad_control_token_for_user_id(token_user_id)
    if not token:
        return {"account_id": account_id, "items": [], "errors": [{"reason": "missing_meta_token", "token_user_id": token_user_id}]}
    if not whitelist:
        return {"account_id": account_id, "items": [], "errors": [{"reason": "no_product_campaign_whitelist"}]}
    active_campaigns = ad_control_meta_active_campaigns(token, account_id)
    active_by_id = {str(item.get("id") or "").strip(): item for item in active_campaigns}
    campaign_ids = [campaign_id for campaign_id in whitelist.keys() if campaign_id in active_by_id]
    campaign_ids = campaign_ids[:AD_CONTROL_MAX_LIVE_CAMPAIGNS]
    starts = {}
    missing = []
    for campaign_id in campaign_ids:
        start = ad_control_campaign_start(scope["product"], account_id, campaign_id)
        if start.get("campaign_start_at"):
            starts[campaign_id] = start
        else:
            missing.append(campaign_id)
    metrics_by_campaign = {}
    by_window = {}
    for campaign_id, start in starts.items():
        window = ad_control_metric_window({}, scope.get("window"), start.get("campaign_start_at"))
        by_window.setdefault((window["since"], window["until"]), []).append(campaign_id)
    for (since, until), ids in by_window.items():
        metrics_by_campaign.update(ad_control_meta_account_insights(token, account_id, ids, since, until))
    items = []
    for campaign_id in campaign_ids:
        campaign = active_by_id.get(campaign_id) or {}
        whitelist_item = whitelist.get(campaign_id) or {}
        start = starts.get(campaign_id) or {"reason": "missing_campaign_start_at"}
        age_hours = ad_control_age_hours(start.get("campaign_start_at"))
        metrics = metrics_by_campaign.get(campaign_id) or {}
        item = {
            "product": scope["product"],
            "level": "campaign",
            "account_id": account_id,
            "campaign_id": campaign_id,
            "object_id": campaign_id,
            "object_key": "%s:campaign:%s:%s" % (scope["product"], account_id, campaign_id),
            "campaign_name": campaign.get("name") or whitelist_item.get("campaign_name", ""),
            "country": whitelist_item.get("country", ""),
            "language": whitelist_item.get("language", ""),
            "account_time_zone": whitelist_item.get("account_time_zone", ""),
            "status": campaign.get("status", ""),
            "effective_status": campaign.get("effective_status", ""),
            "campaign_start": start,
            "campaign_start_at": start.get("campaign_start_at", ""),
            "age_hours": age_hours,
            "metrics": metrics,
            "token_user_id": token_user_id,
            "skip_reason": "" if age_hours is not None else "missing_campaign_start_at",
        }
        decision = ad_control_evaluate_rules(item, scope.get("rules") or []) if age_hours is not None else {"matched_rules": [], "target_action": "none"}
        item.update(decision)
        items.append(item)
    return {
        "account_id": account_id,
        "items": items,
        "errors": [],
        "active_count": len(active_campaigns),
        "candidate_count": len(campaign_ids),
        "missing_start_count": len(missing),
    }


def create_ad_control_live_preview(payload, session):
    ensure_ad_control_tables()
    scope = ad_control_resolve_live_scope(payload or {})
    resource = ad_control_resource_snapshot()
    whitelist_by_account = ad_control_product_campaign_whitelist(scope["product"], scope["account_ids"])
    token_configs = ad_control_token_config_for_accounts(scope["product"], scope["account_ids"])
    workers = min(max(1, AD_CONTROL_LIVE_MAX_WORKERS), len(scope["account_ids"]))
    if resource.get("over_limit"):
        workers = 1
    account_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {}
        for account_id in scope["account_ids"]:
            future = executor.submit(
                ad_control_collect_live_account,
                scope,
                account_id,
                token_configs.get(account_id) or {},
                whitelist_by_account.get(account_id) or {},
            )
            future_map[future] = account_id
        for future in concurrent.futures.as_completed(future_map):
            account_id = future_map[future]
            try:
                account_results.append(future.result())
            except Exception as exc:
                logging.exception("ad control live preview account failed: %s", account_id)
                account_results.append({"account_id": account_id, "items": [], "errors": [{"reason": str(exc)}]})
    items = []
    errors = []
    for result in account_results:
        items.extend(result.get("items") or [])
        for err in result.get("errors") or []:
            err["account_id"] = result.get("account_id")
            errors.append(err)
    total = len(items)
    pause_count = len([item for item in items if item.get("target_action") == "pause"])
    observe_count = len([item for item in items if item.get("target_action") == "observe"])
    preview_id = uuid.uuid4().hex
    preview_hash = ad_control_rule_hash({
        "product": scope["product"],
        "accounts": scope["account_ids"],
        "rules": scope["rules"],
        "window": scope.get("window"),
        "strategy": scope.get("strategy") or {},
        "rule_group_id": scope.get("rule_group_id"),
        "binding_id": scope.get("rule_group_id"),
    })
    expires_at = (datetime.utcnow() + timedelta(seconds=AD_CONTROL_PREVIEW_TTL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
    criteria = {
        "mode": "live",
        "product": scope["product"],
        "accounts": scope["account_ids"],
        "rules": scope["rules"],
        "window": scope.get("window"),
        "strategy": scope.get("strategy") or {},
        "rule_group_id": scope.get("rule_group_id"),
        "binding_id": scope.get("rule_group_id"),
        "preview_hash": preview_hash,
    }
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO ad_control_preview (
                  preview_id, actor_user_id, action, level, product, criteria_json,
                  sample_json, total_count, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                """,
                (
                    preview_id,
                    ad_control_actor(session),
                    "pause",
                    "campaign",
                    scope["product"],
                    json.dumps(criteria, ensure_ascii=False),
                    json.dumps(items[:AD_CONTROL_MAX_LIVE_EXECUTE], ensure_ascii=False),
                    total,
                    expires_at,
                ),
            )
            if scope.get("rule_group_id"):
                conn.execute(
                    """
                    UPDATE ad_control_rule_group
                       SET last_preview_id=?, last_preview_hash=?, updated_at=CURRENT_TIMESTAMP
                     WHERE group_id=?
                    """,
                    (preview_id, preview_hash, scope.get("rule_group_id")),
                )
            conn.commit()
        finally:
            conn.close()
    return {
        "preview_id": preview_id,
        "expires_at": expires_at,
        "preview_hash": preview_hash,
        "product": scope["product"],
        "account_count": len(scope["account_ids"]),
        "total": total,
        "pause_count": pause_count,
        "observe_count": observe_count,
        "error_count": len(errors),
        "resource": resource,
        "strategy": scope.get("strategy") or {},
        "items": items[:200],
        "errors": errors[:100],
        "remaining_count": max(0, total - min(total, 200)),
    }


def execute_ad_control_live(payload, session):
    ensure_ad_control_tables()
    preview = fetch_ad_control_preview(payload.get("preview_id"))
    criteria = ad_control_safe_json_dict(preview.get("criteria_json"))
    if criteria.get("mode") != "live":
        raise StructuredApiError("invalid_preview", "preview is not a live preview")
    expected_hash = str(criteria.get("preview_hash") or "").strip()
    confirmed_hash = str(payload.get("preview_hash") or "").strip()
    if not expected_hash or confirmed_hash != expected_hash:
        raise StructuredApiError("preview_hash_mismatch", "preview hash confirmation is required")
    dry_run = bool(payload.get("dry_run", True))
    if not dry_run and str(payload.get("confirm") or "") != "EXECUTE_LIVE_PAUSE":
        raise StructuredApiError("confirm_required", "explicit confirmation required")
    items = ad_control_safe_json_list(preview.get("sample_json"))
    action_id = uuid.uuid4().hex
    results = []
    success_count = skipped_count = error_count = 0
    token_configs = ad_control_token_config_for_accounts(criteria.get("product"), criteria.get("accounts") or [])
    for item in items[:AD_CONTROL_MAX_LIVE_EXECUTE]:
        if item.get("target_action") != "pause":
            skipped_count += 1
            results.append({"object_key": item.get("object_key"), "status": "skipped", "reason": "not_pause_target"})
            continue
        if item.get("skip_reason"):
            skipped_count += 1
            results.append({"object_key": item.get("object_key"), "status": "skipped", "reason": item.get("skip_reason")})
            continue
        account_id = ad_control_normalize_account(item.get("account_id"))
        token_user_id = str((token_configs.get(account_id) or {}).get("user_id") or "").strip()
        token = ad_control_token_for_user_id(token_user_id)
        if not token:
            skipped_count += 1
            results.append({"object_key": item.get("object_key"), "status": "skipped", "reason": "missing_meta_token"})
            continue
        try:
            meta = ad_control_graph_get(token, item.get("campaign_id"), "account_id,status,effective_status,name")
            meta_account = ad_control_normalize_account(meta.get("account_id"))
            if meta_account and meta_account != account_id:
                skipped_count += 1
                results.append({"object_key": item.get("object_key"), "status": "skipped", "reason": "account_owner_mismatch"})
                continue
            if str(meta.get("effective_status") or "").upper() != "ACTIVE":
                skipped_count += 1
                results.append({"object_key": item.get("object_key"), "status": "skipped", "reason": "not_active"})
                continue
            if dry_run:
                success_count += 1
                results.append({"object_key": item.get("object_key"), "status": "dry_run", "meta": meta})
                continue
            payload_result = ad_control_graph_set_status(token, item.get("campaign_id"), "PAUSED")
            ad_control_update_business_status({
                "level": "campaign",
                "product": criteria.get("product"),
                "account_id": account_id,
                "object_id": item.get("campaign_id"),
            }, "PAUSED")
            ad_control_save_object_state(action_id, {
                "object_key": item.get("object_key"),
                "product": criteria.get("product"),
                "level": "campaign",
                "account_id": account_id,
                "object_id": item.get("campaign_id"),
                "campaign_id": item.get("campaign_id"),
            }, "paused")
            success_count += 1
            results.append({"object_key": item.get("object_key"), "status": "success", "meta": payload_result})
        except Exception as exc:
            error_count += 1
            logging.exception("ad control live execute failed: %s", item.get("object_key"))
            results.append({"object_key": item.get("object_key"), "status": "error", "reason": str(exc)})
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO ad_control_action (
                  action_id, preview_id, actor_user_id, action, level, product, criteria_json,
                  requested_count, success_count, skipped_count, error_count, dry_run,
                  results_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    action_id,
                    preview["preview_id"],
                    ad_control_actor(session),
                    "pause",
                    "campaign",
                    criteria.get("product", ""),
                    json.dumps(criteria, ensure_ascii=False),
                    len(items),
                    success_count,
                    skipped_count,
                    error_count,
                    1 if dry_run else 0,
                    json.dumps(results, ensure_ascii=False),
                ),
            )
            if criteria.get("rule_group_id"):
                conn.execute(
                    """
                    UPDATE ad_control_rule_group
                       SET last_run_at=CURRENT_TIMESTAMP, last_result_json=?, updated_at=CURRENT_TIMESTAMP
                     WHERE group_id=?
                    """,
                    (
                        json.dumps({"action_id": action_id, "success_count": success_count, "skipped_count": skipped_count, "error_count": error_count, "dry_run": dry_run}, ensure_ascii=False),
                        criteria.get("rule_group_id"),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    return {
        "action_id": action_id,
        "preview_id": preview["preview_id"],
        "dry_run": dry_run,
        "requested_count": len(items),
        "success_count": success_count,
        "skipped_count": skipped_count,
        "error_count": error_count,
        "results": results[:200],
    }


def refresh_ad_control_campaign_start(payload):
    product = str(payload.get("product") or "").strip()
    account_id = ad_control_normalize_account(payload.get("account_id"))
    campaign_id = str(payload.get("campaign_id") or "").strip()
    if not product or not account_id or not campaign_id:
        raise StructuredApiError("missing_campaign", "product, account_id and campaign_id are required")
    ad_control_delete_cached_campaign_start(product, account_id, campaign_id)
    return ad_control_campaign_start(product, account_id, campaign_id, refresh=True)


def lookup_admin_group_by_email(email):
    email = str(email or "").strip()
    database = ADMIN_MAPPING_MYSQL_DATABASE or DB_NAME
    if not email or not database:
        return {}
    try:
        columns = mysql_table_columns("admin_user_group", database)
        if "email" not in columns or "sub_user_id" not in columns:
            return {}
        status_filter = " AND status = 0" if "status" in columns else ""
        rows = run_mysql(
            "SELECT CAST(sub_user_id AS CHAR), email FROM `%s`.admin_user_group WHERE email='%s'%s LIMIT 1"
            % (database.replace("`", "``"), mysql_escape_literal(email), status_filter)
        )
        if rows:
            return {"sub_user_id": str(rows[0][0] or "").strip(), "email": str(rows[0][1] or "").strip()}
    except Exception:
        logging.exception("failed to lookup admin_user_group by email")
    return {}


def lookup_admin_group_by_name(name):
    name = str(name or "").strip()
    database = ADMIN_MAPPING_MYSQL_DATABASE or DB_NAME
    if not name or not database:
        return {}
    try:
        columns = mysql_table_columns("admin_user_group", database)
        if "name" not in columns or "sub_user_id" not in columns:
            return {}
        status_filter = " AND status = 0" if "status" in columns else ""
        rows = run_mysql(
            "SELECT CAST(sub_user_id AS CHAR), email, name FROM `%s`.admin_user_group WHERE name='%s'%s LIMIT 2"
            % (database.replace("`", "``"), mysql_escape_literal(name), status_filter)
        )
        if len(rows) == 1:
            return {
                "sub_user_id": str(rows[0][0] or "").strip(),
                "email": str(rows[0][1] or "").strip(),
                "name": str(rows[0][2] or "").strip(),
            }
    except Exception:
        logging.exception("failed to lookup admin_user_group by name")
    return {}


def lookup_admin_group_for_actor(actor):
    actor = actor or {}
    return lookup_admin_group_by_email(actor.get("email")) or lookup_admin_group_by_name(actor.get("name"))


def product_name_expr(columns):
    candidates = ["app_name", "product_name", "name", "app", "title"]
    parts = ["NULLIF(%s, '')" % sql_identifier(item) for item in candidates if item in columns]
    parts.append("CAST(app_id AS CHAR)")
    return "COALESCE(%s)" % ", ".join(parts)


def product_optional_expr(columns, candidates):
    parts = ["NULLIF(a.%s, '')" % sql_identifier(item) for item in candidates if item in columns]
    return "COALESCE(%s, '')" % ", ".join(parts) if parts else "''"


def mysql_csv_contains_expr(csv_expr, value_expr):
    return "FIND_IN_SET(%s, REPLACE(REPLACE(REPLACE(REPLACE(%s, '[', ''), ']', ''), '\"', ''), ' ', '')) > 0" % (value_expr, csv_expr)


def ad_material_product_select_exprs(columns):
    return {
        "store_url": product_optional_expr(columns, ["store_url", "ios_store_url", "website_url", "origin_websit_url", "click_url"]),
        "package_name": product_optional_expr(columns, ["package", "package_name", "ios_package_name", "package_ios", "google_app_android", "app_id"]),
        "product_icon_url": product_optional_expr(columns, ["icon_url", "profile_image", "profile_image_ios"]),
        "country": product_optional_expr(columns, ["country", "countries", "market", "region", "geo"]),
        "language": product_optional_expr(columns, ["language", "lang", "language_code", "locale"]),
    }


def ad_material_google_store_url(package_name, country="", language=""):
    package_name = str(package_name or "").strip()
    if not package_name:
        return ""
    params = {"id": package_name}
    if language:
        params["hl"] = str(language).strip()
    if country:
        params["gl"] = str(country).strip().upper()
    return "https://play.google.com/store/apps/details?%s" % urlencode(params)


def ad_material_store_icon_from_url(store_url, package_name="", country="", language=""):
    import html as html_lib

    store_url = str(store_url or "").strip()
    package_name = str(package_name or "").strip()
    if not store_url and package_name:
        store_url = ad_material_google_store_url(package_name, country, language)
    if not store_url:
        return ""
    cache_key = "|".join([store_url, package_name, str(country or ""), str(language or "")])
    cache = getattr(ad_material_store_icon_from_url, "_cache", {})
    if cache_key in cache:
        return cache[cache_key]
    icon_url = ""
    try:
        response = requests.get(
            store_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept-Language": "%s,%s;q=0.9,en;q=0.8" % (language or "en", country or "US"),
            },
            timeout=15,
        )
        response.raise_for_status()
        html_text = response.text or ""
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<img[^>]+itemprop=["\']image["\'][^>]+src=["\']([^"\']+)["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, html_text, flags=re.I)
            if match:
                icon_url = html_lib.unescape(match.group(1)).strip()
                break
        if icon_url.startswith("//"):
            icon_url = "https:" + icon_url
    except Exception:
        logging.exception("failed to fetch store icon: %s", store_url)
        icon_url = ""
    cache[cache_key] = icon_url
    setattr(ad_material_store_icon_from_url, "_cache", cache)
    return icon_url


def enrich_ad_material_store_icon(data):
    icon_url = ad_material_store_icon_from_url(
        data.get("store_url", ""),
        data.get("package_name", ""),
        data.get("country", ""),
        data.get("language", ""),
    )
    data["product_icon_url"] = icon_url or ""
    return data


def screenshot_failure_notify_names():
    text = os.environ.get("SCREENSHOT_FAILURE_NOTIFY_NAMES", "\u90dc\u8fdc")
    return [item.strip() for item in text.split(",") if item.strip()]


def screenshot_failure_notify_recipients():
    names = screenshot_failure_notify_names()
    if not names:
        return []
    placeholders = ",".join("?" for _ in names)
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT user_id, open_id, name
                FROM drama_admin_user
                WHERE name IN (%s)
                  AND (TRIM(user_id) != '' OR TRIM(open_id) != '')
                """
                % placeholders,
                names,
            ).fetchall()
        finally:
            conn.close()
    recipients = []
    seen = set()
    for row in rows:
        user_id = str(row["user_id"] or "").strip()
        open_id = str(row["open_id"] or "").strip()
        name = str(row["name"] or "").strip()
        if user_id:
            key = ("user_id", user_id)
            if key not in seen:
                seen.add(key)
                recipients.append({"receive_id_type": "user_id", "receive_id": user_id, "name": name})
        elif open_id:
            key = ("open_id", open_id)
            if key not in seen:
                seen.add(key)
                recipients.append({"receive_id_type": "open_id", "receive_id": open_id, "name": name})
    return recipients


def screenshot_failed_size_labels(error_text):
    text = str(error_text or "")
    labels = []
    for label in ("1.91:1 横图", "1:1 方图", "4:5 竖图"):
        if ("%s:" % label) in text or ("%s：" % label) in text:
            labels.append(label)
    return labels


def humanize_screenshot_failure_reason(job, error_text):
    raw_reason = (
        str(error_text or "").strip()
        or str(job.get("error_message", "") or "").strip()
    )
    lower_reason = raw_reason.lower()
    failed_labels = screenshot_failed_size_labels(raw_reason)
    failed_suffix = "，失败尺寸：%s" % "、".join(failed_labels) if failed_labels else ""

    if is_screenshot_generation_no_output_error(raw_reason):
        return (
            "图片生成工具在“出图前”返回 UserError，未生成原始图片文件%s。\n"
            "为什么没有图片：失败发生在 AI 图片生成/编辑工具内部，工具没有写出 raw_generated_path，"
            "所以系统没有任何可校验、可裁切或可上传的图片。\n"
            "为什么重试仍失败：系统已先做批量生成；批量失败后又降级为按尺寸单独生成，"
            "但这些失败尺寸使用同一张源封面和同一组源图锁定生成要求再次调用时，仍在出图前返回同类 UserError。"
            "这类错误不是认证、排队、下载、上传或比例校验问题，继续按相同输入自动重试只会重复失败。\n"
            "底层工具没有返回更细的拒绝码，无法再区分是源图内容、人物/标题锁定要求，还是其他模型侧限制；"
            "建议更换源封面，或放宽源图锁定要求后重新制作。"
            % failed_suffix
        )
    if "downloaded screenshot source image is invalid" in lower_reason or "source image is invalid" in lower_reason:
        return "源封面图片下载后不是有效图片，系统无法读取原图；建议更换可正常打开的封面链接。"
    if is_screenshot_source_consistency_rejection(raw_reason):
        return (
            "生成图和源封面差异过大，人物、标题或主体一致性校验未通过%s；"
            "系统已停止继续使用这版结果。"
            % failed_suffix
        )
    if "raw aspect ratio rejected" in lower_reason or "aspect ratio" in lower_reason:
        return "生成图尺寸比例不符合投放要求%s，系统已拦截该结果。" % failed_suffix
    if "token_invalidated" in lower_reason or "refresh_token" in lower_reason or "401" in lower_reason:
        return "Codex 登录凭证失效或刷新令牌不可用，图片生成服务无法继续调用，需要重新认证后重试。"
    if "timed out" in lower_reason or "timeout" in lower_reason:
        return "图片生成进程超时%s，系统没有在限定时间内拿到结果。" % failed_suffix
    if (
        "remotedisconnected" in lower_reason
        or "connection refused" in lower_reason
        or "connection aborted" in lower_reason
    ):
        return "截图生成 sidecar 连接中断%s，通常发生在服务重启或 sidecar 短暂不可用时。" % failed_suffix
    first_line = raw_reason.splitlines()[0][:240] if raw_reason else "未知错误"
    return "截图生成失败%s。技术错误：%s" % (failed_suffix, first_line)


def build_screenshot_failure_message(job, error_text):
    reason = humanize_screenshot_failure_reason(job, error_text)
    return (
        "封面图合成任务失败，系统已停止继续重试。\n"
        "剧名：%s\n"
        "content_id：%s\n"
        "job_id：%s\n"
        "失败原因：%s\n"
        "已生成尺寸：1.91:1=%s，1:1=%s，4:5=%s"
        % (
            str(job.get("drama_name", "") or ""),
            str(job.get("content_id", "") or ""),
            str(job.get("job_id", "") or ""),
            reason,
            "有" if str(job.get("landscape_1_91x1_url", "") or "").strip() else "无",
            "有" if str(job.get("square_1x1_url", "") or "").strip() else "无",
            "有" if str(job.get("portrait_4x5_url", "") or "").strip() else "无",
        )
    )


def should_skip_screenshot_failure_notification(job, error_text):
    raw_reason = (
        str(error_text or "").strip()
        or str((job or {}).get("error_message", "") or "").strip()
    )
    return is_screenshot_generation_no_output_error(raw_reason)


def notify_screenshot_failure(job, error_text):
    try:
        if should_skip_screenshot_failure_notification(job, error_text):
            logging.info(
                "skip screenshot failure notification for raw_generated_path no-output error: %s",
                job.get("job_id", ""),
            )
            try:
                append_audit_log(
                    None,
                    "notify_screenshot_failure_skipped",
                    "screenshot_job",
                    job.get("job_id", ""),
                    {"reason": "generation_no_output_raw_generated_path"},
                )
            except Exception:
                logging.exception("failed to write screenshot failure notify skipped audit log")
            return
        recipients = screenshot_failure_notify_recipients()
        if not recipients:
            logging.warning("no screenshot failure Feishu recipients configured")
            try:
                append_audit_log(
                    None,
                    "notify_screenshot_failure_skipped",
                    "screenshot_job",
                    job.get("job_id", ""),
                    {"reason": "missing_recipient", "names": screenshot_failure_notify_names()},
                )
            except Exception:
                logging.exception("failed to write screenshot failure notify skipped audit log")
            return
        message = build_screenshot_failure_message(job, error_text)
        sent = []
        errors = []
        for recipient in recipients:
            try:
                send_feishu_text(recipient["receive_id_type"], recipient["receive_id"], message)
                sent.append(recipient)
            except Exception as exc:
                logging.exception("failed to notify screenshot failure recipient: %s", recipient)
                errors.append({"recipient": recipient, "error": str(exc).strip() or exc.__class__.__name__})
        try:
            append_audit_log(
                None,
                "notify_screenshot_failure",
                "screenshot_job",
                job.get("job_id", ""),
                {"sent": sent, "errors": errors},
            )
        except Exception:
            logging.exception("failed to write screenshot failure notify audit log")
    except Exception:
        logging.exception("failed to notify screenshot failure: %s", job.get("job_id", ""))


def lookup_ad_material_product_metadata(app_id):
    app_id = str(app_id or "").strip()
    if not app_id:
        return {}
    database = ADMIN_MAPPING_MYSQL_DATABASE or DB_NAME
    if not database:
        return {}
    try:
        columns = mysql_table_columns("ads_apps_setting", database)
        if "id" not in columns:
            return {}
        exprs = ad_material_product_select_exprs(columns)
        rows = run_mysql(
            "SELECT CAST(a.id AS CHAR), a.name, %s, %s, %s, %s, %s "
            "FROM `%s`.ads_apps_setting a WHERE CAST(a.id AS CHAR) = '%s' LIMIT 1"
            % (
                exprs["store_url"],
                exprs["package_name"],
                exprs["product_icon_url"],
                exprs["country"],
                exprs["language"],
                database.replace("`", "``"),
                mysql_escape_literal(app_id),
            )
        )
        if not rows:
            return {}
        row = rows[0]
        return {
            "app_id": str(row[0] if len(row) > 0 else "").strip(),
            "product_name": str(row[1] if len(row) > 1 else "").strip(),
            "store_url": str(row[2] if len(row) > 2 else "").strip(),
            "package_name": str(row[3] if len(row) > 3 else "").strip(),
            "product_icon_url": str(row[4] if len(row) > 4 else "").strip(),
            "country": str(row[5] if len(row) > 5 else "").strip(),
            "language": str(row[6] if len(row) > 6 else "").strip(),
        }
    except Exception:
        logging.exception("failed to lookup ad material product metadata: %s", app_id)
        return {}


def normalize_ad_material_product_search_text(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", "", text).lower()


def ad_material_product_matches_query(item, query):
    tokens = [
        normalize_ad_material_product_search_text(part)
        for part in re.split(r"\s+", str(query or "").strip())
        if str(part or "").strip()
    ]
    if not tokens:
        return True
    text = normalize_ad_material_product_search_text(" ".join([
        str(item.get("app_id", "") or ""),
        str(item.get("id", "") or ""),
        str(item.get("product_name", "") or ""),
        str(item.get("name", "") or ""),
        str(item.get("label", "") or ""),
        str(item.get("package_name", "") or ""),
        str(item.get("store_url", "") or ""),
    ]))
    return all(token in text for token in tokens)


def list_ad_material_products(session=None, query="", limit=None, with_total=False):
    search_query = str(query or "").strip()
    try:
        limit_value = int(limit) if limit not in (None, "") else 0
    except Exception:
        limit_value = 0
    limit_value = max(0, min(500, limit_value))

    def finish(items, total=None):
        total = len(items) if total is None else int(total or 0)
        if limit_value:
            items = items[:limit_value]
        if with_total:
            return {"items": items, "total": total}
        return items

    database = ADMIN_MAPPING_MYSQL_DATABASE or DB_NAME
    if database:
        try:
            columns = mysql_table_columns("ads_apps_setting", database)
            if "id" in columns and "name" in columns:
                where = "1=1"
                is_admin = session_is_admin(session)
                if not is_admin:
                    actor = ad_material_actor(session)
                    admin_group = lookup_admin_group_for_actor(actor)
                    sub_user_id = admin_group.get("sub_user_id")
                    where = "0=1"
                    role_app_columns = mysql_table_columns("admin_role_apps", database)
                    role_user_columns = mysql_table_columns("admin_role_users", database)
                    if sub_user_id and {"role_id", "user_id"}.issubset(role_user_columns) and {"role_id", "is_all", "values"}.issubset(role_app_columns):
                        join_condition = "ara.role_id = aru.role_id"
                        if "role_app_id" in role_user_columns and "id" in role_app_columns:
                            join_condition = "ara.id = aru.role_app_id"
                        where = (
                            "EXISTS (SELECT 1 FROM `%s`.admin_role_users aru "
                            "JOIN `%s`.admin_role_apps ara ON %s "
                            "WHERE CAST(aru.user_id AS CHAR) = '%s' "
                            "AND (ara.is_all = 1 OR %s))"
                        ) % (
                            database.replace("`", "``"),
                            database.replace("`", "``"),
                            join_condition,
                            mysql_escape_literal(sub_user_id),
                            mysql_csv_contains_expr("ara.values", "CAST(a.id AS CHAR)"),
                        )
                exprs = ad_material_product_select_exprs(columns)
                search_fields = ["CAST(a.id AS CHAR)"]
                for column in ("name", "app_name", "product_name", "app", "title", "package", "package_name", "ios_package_name", "google_app_android"):
                    if column in columns:
                        search_fields.append("CAST(a.%s AS CHAR)" % sql_identifier(column))
                query_where = where
                if search_query:
                    like = "%%%s%%" % mysql_escape_literal(search_query)
                    search_sql = " OR ".join("LOWER(%s) LIKE LOWER('%s')" % (field, like) for field in search_fields)
                    query_where = "(%s) AND (%s)" % (where, search_sql)
                total = 0
                try:
                    total_rows = run_mysql(
                        "SELECT COUNT(DISTINCT a.id) FROM `%s`.ads_apps_setting a WHERE %s"
                        % (database.replace("`", "``"), query_where)
                    )
                    total = int(total_rows[0][0]) if total_rows and total_rows[0] else 0
                except Exception:
                    logging.exception("failed to count ad material products")
                limit_clause = " LIMIT %d" % limit_value if limit_value else ""
                rows = run_mysql(
                    "SELECT DISTINCT CAST(a.id AS CHAR), a.name, %s, %s, %s, %s, %s "
                    "FROM `%s`.ads_apps_setting a WHERE %s ORDER BY 2 ASC%s"
                    % (
                        exprs["store_url"],
                        exprs["package_name"],
                        exprs["product_icon_url"],
                        exprs["country"],
                        exprs["language"],
                        database.replace("`", "``"),
                        query_where,
                        limit_clause,
                    )
                )
                items = []
                for row in rows:
                    app_id = str(row[0] if len(row) > 0 else "").strip()
                    product_name = str(row[1] if len(row) > 1 else "").strip() or app_id
                    if app_id:
                        items.append({
                            "app_id": app_id,
                            "id": app_id,
                            "product_name": product_name,
                            "name": product_name,
                            "country": str(row[5] if len(row) > 5 else "").strip(),
                            "language": str(row[6] if len(row) > 6 else "").strip(),
                            "store_url": str(row[2] if len(row) > 2 else "").strip(),
                            "package_name": str(row[3] if len(row) > 3 else "").strip(),
                            "product_icon_url": str(row[4] if len(row) > 4 else "").strip(),
                            "label": "%s | %s" % (app_id, product_name),
                        })
                return finish(items, total if total else len(items))
        except Exception:
            logging.exception("failed to list ad material products from mysql")
    try:
        items = [
            {
                "app_id": item.get("app_id", ""),
                "id": item.get("app_id", ""),
                "product_name": item.get("app", "") or item.get("label", "") or item.get("app_id", ""),
                "name": item.get("app", "") or item.get("label", "") or item.get("app_id", ""),
                "country": item.get("country", ""),
                "language": item.get("language", ""),
                "store_url": item.get("store_url", ""),
                "package_name": item.get("package", "") or item.get("package_name", ""),
                "product_icon_url": item.get("icon_url", "") or item.get("product_icon_url", ""),
                "label": item.get("label", "") or item.get("app_id", ""),
            }
            for item in list_products()
        ]
        if search_query:
            items = [item for item in items if ad_material_product_matches_query(item, search_query)]
        return finish(items, len(items))
    except Exception:
        logging.exception("failed to list fallback products")
        return finish([], 0)


def save_ad_material_reference_files(task_id, raw_files):
    saved = []
    if not isinstance(raw_files, list):
        return saved
    target_dir = os.path.join(ad_material_task_work_dir(task_id), "references")
    public_dir = os.path.join(ad_material_public_dir(task_id), "references")
    ensure_dir(target_dir)
    ensure_dir(public_dir)
    for index, item in enumerate(raw_files, 1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "reference_%02d" % index).strip()
        name = re.sub(r"[\\/:*?\"<>|]+", "-", name).strip(". ") or "reference_%02d" % index
        data_url = str(item.get("data_url", "") or "")
        raw_base64 = str(item.get("base64", "") or "")
        if data_url and "," in data_url:
            raw_base64 = data_url.split(",", 1)[1]
        if not raw_base64:
            continue
        data = base64.b64decode(raw_base64)
        if len(data) > 20 * 1024 * 1024:
            raise StructuredApiError("reference_file_too_large", "单个参考素材不能超过20MB")
        path = os.path.join(target_dir, "%02d_%s" % (index, name))
        public_path = os.path.join(public_dir, "%02d_%s" % (index, name))
        with open(path, "wb") as handle:
            handle.write(data)
        with open(public_path, "wb") as handle:
            handle.write(data)
        content_type = str(item.get("content_type", "") or guess_content_type(name))
        saved.append({
            "name": name,
            "content_type": content_type,
            "local_path": path,
            "public_path": public_path,
            "url": build_public_url(public_path),
            "size": len(data),
        })
    return saved


def ad_material_task_from_row(row):
    item = dict(row)
    item["reference_files"] = parse_json_text(item.pop("reference_files_json", "[]"), [])
    item["demand_artifacts"] = parse_json_text(item.pop("demand_artifacts_json", "{}"), {})
    item["status_label"] = AD_MATERIAL_STATUS_LABELS.get(item.get("status"), item.get("status", ""))
    item["quantity"] = int(item.get("quantity") or 0)
    item["task_type"] = normalize_ad_material_task_type(item.get("task_type"))
    item["size_plan"] = ad_material_size_plan_from_task(item)
    item["size_plan_summary"] = format_ad_material_size_plan(item["size_plan"])
    item["assets"] = fetch_ad_material_assets(item["task_id"])
    return item


def ad_material_asset_from_row(row):
    item = dict(row)
    item["status_label"] = AD_MATERIAL_ASSET_STATUS_LABELS.get(item.get("status"), item.get("status", ""))
    return item


def fetch_ad_material_assets(task_id):
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM ad_material_asset WHERE task_id = ? ORDER BY asset_index ASC, created_at ASC",
                (task_id,),
            ).fetchall()
            return [ad_material_asset_from_row(row) for row in rows]
        finally:
            conn.close()


def fetch_ad_material_task(task_id):
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            row = conn.execute("SELECT * FROM ad_material_task WHERE task_id = ?", (task_id,)).fetchone()
        finally:
            conn.close()
    return ad_material_task_from_row(row) if row else None


def ensure_ad_material_access(session, task):
    if not task:
        raise StructuredApiError("not_found", "任务不存在")
    if session_is_admin(session):
        return
    actor = ad_material_actor(session)
    if task.get("creator_user_id") == actor.get("user_id"):
        return
    raise PermissionError("permission_denied")


def validate_ad_material_payload(payload, existing=None):
    payload = payload or {}
    task_type = normalize_ad_material_task_type(payload.get("task_type", existing.get("task_type") if existing else ""))
    size_plan = normalize_ad_material_size_plan(payload, existing)
    quantity = sum(int(item["count"]) for item in size_plan)
    app_id = str(payload.get("app_id", existing.get("app_id") if existing else "") or "").strip()
    country = str(payload.get("country", existing.get("country") if existing else "") or "").strip().upper()
    language = str(payload.get("language", existing.get("language") if existing else "") or "").strip().lower()
    if not app_id:
        raise StructuredApiError("invalid_app_id", "产品不能为空")
    if not country:
        raise StructuredApiError("invalid_country", "国家不能为空")
    if not language:
        raise StructuredApiError("invalid_language", "语言不能为空")
    return {
        "task_type": task_type,
        "competitor_source": normalize_competitor_source(task_type, payload.get("competitor_source", existing.get("competitor_source") if existing else "")),
        "app_id": app_id,
        "product_name": str(payload.get("product_name", existing.get("product_name") if existing else "") or "").strip(),
        "country": country,
        "language": language,
        "size": format_ad_material_size_plan(size_plan),
        "size_plan": size_plan,
        "tag_name": str(payload.get("tag_name", existing.get("tag_name") if existing else "") or "").strip(),
        "category": str(payload.get("category", existing.get("category") if existing else "") or "").strip(),
        "title": str(payload.get("title", existing.get("title") if existing else "") or "").strip(),
        "body": str(payload.get("body", existing.get("body") if existing else "") or "").strip(),
        "description": str(payload.get("description", existing.get("description") if existing else "") or "").strip(),
        "store_url": str(payload.get("store_url", existing.get("store_url") if existing else "") or "").strip(),
        "package_name": str(payload.get("package_name", existing.get("package_name") if existing else "") or "").strip(),
        "product_icon_url": str(payload.get("product_icon_url", existing.get("product_icon_url") if existing else "") or "").strip(),
        "quantity": quantity,
    }


def create_ad_material_task(payload, session):
    actor = ad_material_actor(session)
    task_id = uuid.uuid4().hex
    data = validate_ad_material_payload(payload)
    products = [] if data["product_name"] else list_ad_material_products(session)
    product = next((item for item in products if str(item.get("app_id")) == data["app_id"]), {})
    product_meta = lookup_ad_material_product_metadata(data["app_id"])
    if not data["product_name"]:
        data["product_name"] = product.get("product_name") or product_meta.get("product_name") or product.get("label") or data["app_id"]
    for key in ("store_url", "package_name", "product_icon_url"):
        if not data.get(key):
            data[key] = product.get(key) or product_meta.get(key) or ""
    enrich_ad_material_store_icon(data)
    admin_group = lookup_admin_group_for_actor(actor)
    creator_email = actor["email"] or admin_group.get("email", "")
    references = save_ad_material_reference_files(task_id, payload.get("reference_files", []))
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO ad_material_task (
                  task_id, task_type, competitor_source, app_id, product_name, country, language,
                  size, tag_name, category, title, body, description, store_url, package_name, product_icon_url,
                  quantity, reference_files_json,
                  status, creator_user_id, creator_open_id, creator_email, creator_name, initiator_sub_user_id,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    task_id, data["task_type"], data["competitor_source"], data["app_id"], data["product_name"],
                    data["country"], data["language"], data["size"], data["tag_name"], data["category"],
                    data["title"], data["body"], data["description"], data["store_url"], data["package_name"],
                    data["product_icon_url"], data["quantity"], json.dumps(references, ensure_ascii=False),
                    actor["user_id"], actor["open_id"], creator_email, actor["name"], admin_group.get("sub_user_id", ""),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return fetch_ad_material_task(task_id)


def update_ad_material_task(task_id, payload, session):
    task = fetch_ad_material_task(task_id)
    ensure_ad_material_access(session, task)
    if task["status"] != "draft":
        raise StructuredApiError("task_locked", "任务发布后不允许编辑")
    data = validate_ad_material_payload(payload, task)
    product_meta = lookup_ad_material_product_metadata(data["app_id"])
    for key in ("store_url", "package_name", "product_icon_url"):
        if not data.get(key):
            data[key] = product_meta.get(key, "")
    enrich_ad_material_store_icon(data)
    references = task.get("reference_files", [])
    new_refs = save_ad_material_reference_files(task_id, payload.get("reference_files", []))
    if new_refs:
        references = references + new_refs
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                """
                UPDATE ad_material_task
                SET task_type=?, competitor_source=?, app_id=?, product_name=?, country=?, language=?,
                    size=?, tag_name=?, category=?, title=?, body=?, description=?, store_url=?, package_name=?,
                    product_icon_url=?, quantity=?,
                    reference_files_json=?, updated_at=CURRENT_TIMESTAMP
                WHERE task_id=?
                """,
                (
                    data["task_type"], data["competitor_source"], data["app_id"], data["product_name"],
                    data["country"], data["language"], data["size"], data["tag_name"], data["category"],
                    data["title"], data["body"], data["description"], data["store_url"], data["package_name"],
                    data["product_icon_url"], data["quantity"],
                    json.dumps(references, ensure_ascii=False), task_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return fetch_ad_material_task(task_id)


def list_ad_material_tasks(session, params):
    page = max(1, int((params.get("page") or ["1"])[0] or "1"))
    page_size = max(1, min(100, int((params.get("page_size") or ["20"])[0] or "20")))
    where = []
    args = []
    for field in ("status", "task_type", "app_id", "country", "language"):
        value = str((params.get(field) or [""])[0] or "").strip()
        if value and value != "all":
            where.append("%s = ?" % field)
            args.append(value)
    query = str((params.get("q") or [""])[0] or "").strip()
    if query:
        like = "%%%s%%" % query
        where.append("(task_id LIKE ? OR product_name LIKE ? OR description LIKE ? OR creator_name LIKE ?)")
        args.extend([like, like, like, like])
    if not session_is_admin(session):
        where.append("creator_user_id = ?")
        args.append(ad_material_actor(session).get("user_id"))
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            total = conn.execute("SELECT COUNT(*) FROM ad_material_task%s" % where_sql, args).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM ad_material_task%s ORDER BY updated_at DESC LIMIT ? OFFSET ?" % where_sql,
                args + [page_size, (page - 1) * page_size],
            ).fetchall()
        finally:
            conn.close()
    return {
        "items": [ad_material_task_from_row(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def update_ad_material_task_status(task_id, status, **fields):
    assignments = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
    args = [normalize_ad_material_status(status)]
    for key, value in fields.items():
        assignments.append("%s = ?" % key)
        args.append("" if value is None else value)
    args.append(task_id)
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute("UPDATE ad_material_task SET %s WHERE task_id = ?" % ", ".join(assignments), args)
            conn.commit()
        finally:
            conn.close()


def upsert_ad_material_asset(asset):
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO ad_material_asset (
                  asset_id, task_id, asset_index, name, url, local_path, status,
                  review_reason, source_api_id, source_api_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(asset_id) DO UPDATE SET
                  name=excluded.name,
                  url=excluded.url,
                  local_path=excluded.local_path,
                  status=excluded.status,
                  review_reason=excluded.review_reason,
                  source_api_error=excluded.source_api_error,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    asset["asset_id"], asset["task_id"], int(asset.get("asset_index") or 1), asset.get("name", ""),
                    asset.get("url", ""), asset.get("local_path", ""), asset.get("status", "pending_review"),
                    asset.get("review_reason", ""), asset.get("source_api_id", ""), asset.get("source_api_error", ""),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def run_ad_material_external_command(command, task, stage, extra=None):
    task = dict(task or {})
    product_meta = lookup_ad_material_product_metadata(task.get("app_id", ""))
    for key in ("store_url", "package_name", "product_icon_url"):
        if not task.get(key):
            task[key] = product_meta.get(key, "")
    enrich_ad_material_store_icon(task)
    workdir = ad_material_task_work_dir(task["task_id"])
    ensure_dir(workdir)
    input_path = os.path.join(workdir, "%s_input.json" % stage)
    output_path = os.path.join(workdir, "%s_output.json" % stage)
    payload = {"task": task, "extra": extra or {}, "output_path": output_path}
    with open(input_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    env = os.environ.copy()
    env["AD_MATERIAL_TASK_ID"] = task["task_id"]
    env["AD_MATERIAL_TASK_PAYLOAD"] = input_path
    env["AD_MATERIAL_TASK_OUTPUT"] = output_path
    proc = subprocess.run(
        command,
        shell=True,
        cwd=workdir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=AD_MATERIAL_COMMAND_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "ad material command failed").strip())
    if os.path.isfile(output_path):
        with open(output_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    text = (proc.stdout or "").strip()
    if text.startswith("{"):
        return json.loads(text)
    return {"stdout": text}


def fallback_ad_material_demand(task, reason=""):
    lines = [
        "# %s 投放素材需求" % (task.get("product_name") or task.get("app_id")),
        "",
        "- 任务类型：%s" % task.get("task_type"),
        "- 国家/语言：%s/%s" % (task.get("country"), task.get("language")),
        "- 数量：%s" % task.get("quantity"),
        "- 尺寸：%s" % (task.get("size") or "按投放平台默认比例"),
        "- 竞品查询源：%s" % (task.get("competitor_source") or "不使用"),
        "",
        "## 生成方向",
        task.get("description") or "围绕产品核心卖点生成可投放静态素材，优先复用上传参考素材的构图、色彩和信息层级。",
        "",
        "## 上报字段",
        "- category：%s" % (task.get("category") or ""),
        "- tag_name：%s" % (task.get("tag_name") or ""),
        "- title：%s" % (task.get("title") or ""),
        "- body：%s" % (task.get("body") or ""),
    ]
    if reason:
        lines.extend(["", "## 本次重生成原因", reason])
    return "\n".join(lines)


def _ad_material_size(task):
    size = str(task.get("size_plan_summary") or "").strip()
    if not size:
        size = format_ad_material_size_plan(ad_material_size_plan_from_task(task))
    return size or "按最终投放版位约束；默认优先 1:1 静态图"


def _ad_material_reference_names(task):
    refs = task.get("reference_files") or []
    names = []
    for item in refs:
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _ad_material_reference_items(task):
    refs = task.get("reference_files") or []
    items = []
    for index, item in enumerate(refs, 1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip() or "reference_%02d" % index
        url = str(item.get("url") or item.get("public_url") or "").strip()
        content_type = str(item.get("content_type") or guess_content_type(name)).strip()
        items.append({"name": name, "url": url, "content_type": content_type})
    return items


def normalize_cover_source_url(url):
    text = str(url or "").strip()
    return text


def _ad_material_source_note(task):
    source = str(task.get("competitor_source") or "").strip()
    task_kind = ad_material_task_kind(task.get("task_type"))
    if task_kind == "iteration":
        return "本任务以需求人上传的参考素材和产品自身信息为主，不强制拉取竞品素材。"
    if task_kind == "reference":
        return "本任务以需求人上传的参考素材为核心输入，先解析参考素材的构图、色彩、主体关系和信息层级，再迁移为当前产品的新素材。"
    return "本任务需要通过 %s 拉取并筛选 image-only 竞品素材，最终需求应优先继承通过审核的竞品参考风格。" % (source or "已配置竞品源")


def _ad_material_direction(task):
    description = str(task.get("description") or "").strip()
    if description:
        return description
    task_kind = ad_material_task_kind(task.get("task_type"))
    if task_kind == "iteration":
        return "基于上传参考素材做静态图优化，保留可复用的构图、主体、卖点层级和品牌识别，不直接照搬原图。"
    if task_kind == "reference":
        return "先解析上传参考素材的原素材风格，包括版式、色彩、主体、镜头、文案层级和 CTA 位置，再结合当前产品信息生成新的静态图方案。"
    if task_kind == "competitor":
        return "从竞品静态图中提炼可迁移的版式、信息层级、CTA 和色彩节奏，再替换为当前产品品牌与合规表达。"
    return "综合产品信息、上传参考素材和竞品静态图，产出可审核、可投放、可继续交给图片生成服务执行的静态素材需求。"


def fallback_ad_material_demand_v2(task, reason=""):
    product = task.get("product_name") or task.get("app_id") or "未命名产品"
    app_id = task.get("app_id") or ""
    quantity = max(1, int(task.get("quantity") or 1))
    size = _ad_material_size(task)
    reference_items = _ad_material_reference_items(task)
    reference_names = [item["name"] for item in reference_items]
    source_note = _ad_material_source_note(task)
    direction = _ad_material_direction(task)
    has_refs = bool(reference_names)
    ref_text = "、".join(reference_names) if has_refs else "暂无上传参考素材"
    output_ratio = "1:1" if str(size).lower() in ("", "1080x1080") else size

    lines = [
        "# %s 静态图片素材需求审核版" % product,
        "",
        "> 当前为 Markdown 审核版需求。若后续接入 MetApi / 广大大 / AI 视觉识别脚本，外部脚本返回的 Markdown/PDF 可直接替换本内容。",
        "",
        "## 1. 需求范围与输出规格",
        "",
        "- 产品：%s" % product,
        "- App ID：%s" % app_id,
        "- 任务类型：%s" % (task.get("task_type") or ""),
        "- 市场/语言：%s / %s" % (task.get("country") or "", task.get("language") or ""),
        "- 输出数量：%s 张静态图片素材" % quantity,
        "- 输出尺寸：%s" % size,
        "- 输出比例：%s" % output_ratio,
        "- 投放场景：Meta / Facebook 静态图片素材",
        "- 竞品数据源：%s" % (task.get("competitor_source") or "不使用竞品源"),
        "- 上传参考素材：%s" % ref_text,
        "",
        "## 2. 数据与参考来源",
        "",
        "- 参考策略：%s" % source_note,
        "- 需求方向：%s" % direction,
        "- 审核要求：需求通过后再进入素材生成；未通过时必须填写驳回原因，并按原因重新生成需求。",
        "- 重要限制：不得复制竞品 logo、品牌色、界面细节或不可验证承诺；不得使用保证通过、秒到账、无审核、官方背书等高风险表达。",
        "",
        "## 3. 参考素材识别要求",
        "",
    ]
    if has_refs:
        lines.extend([
            "生成服务必须先逐张识别上传参考素材，再基于识别结果写入最终制作要求：",
            "",
        ])
        for index, name in enumerate(reference_names, 1):
            lines.append("- REF_%02d：%s；需识别可见主体、构图、色彩、文字层级、CTA、可迁移元素与禁止照搬元素。" % (index, name))
    else:
        lines.extend([
            "当前任务没有上传参考素材。正式生成前需要补齐至少一种参考来源：",
            "",
            "- 上传内部高质量静态图素材；或",
            "- 通过已选竞品源拉取并筛选 image-only 竞品素材；或",
            "- 在任务描述中补充明确的画面、文案和品牌规范。",
        ])
    lines.extend(["", "## 4. 逐张素材需求", ""])

    for index in range(1, quantity + 1):
        request_id = "REQ_%02d" % index
        lines.extend([
            "### %s" % request_id,
            "",
            "- 目标：产出 1 张可投放静态图片，必须服务于当前产品和当前市场语言。",
            "- 参考继承：%s" % ("优先继承上传参考素材的版式、主体关系、色彩节奏和信息层级。" if has_refs else "先补齐参考素材或竞品素材，再基于真实识别结论确定视觉方向。"),
            "- 画面结构：保留清晰主视觉区、核心卖点区、CTA 区、品牌/Logo 区；移动端信息层级必须一眼可读。",
            "- 文案要求：主标题、辅助说明、CTA 必须使用 %s 语言；文案应具体、克制、可验证，避免夸张承诺。" % (task.get("language") or "目标市场"),
            "- Logo 规则：必须使用当前产品 logo；如系统无法定位透明 logo，需在画面中预留 logo 位置，不得用竞品 logo 替代。",
            "- 生成方式：AI 负责生成背景、主体、氛围和版式草图；关键文字、Logo、按钮文案应作为可控图层或后置叠加，保证清晰不乱码。",
            "- 验收标准：尺寸符合 %s；文案无拼写错误；主体无遮挡；品牌露出清晰；不出现竞品品牌资产；不出现违规承诺。" % size,
            "",
        ])

    lines.extend([
        "## 5. 上报字段",
        "",
        "- category：%s" % (task.get("category") or ""),
        "- tag_name：%s" % (task.get("tag_name") or ""),
        "- title：%s" % (task.get("title") or ""),
        "- body：%s" % (task.get("body") or ""),
        "- remark：固定留空",
        "",
        "## 6. 审核关注点",
        "",
        "- 每一张素材必须能对应到明确的需求条目和参考来源。",
        "- 如果使用竞品素材，只能学习版式/节奏/信息层级，不得复制品牌资产。",
        "- 如果使用上传参考素材，只能迁移可复用风格，不得直接改色或简单换字。",
        "- 若需求被驳回，下一轮必须围绕驳回原因调整，不保留历史版本。",
    ])
    if reason:
        lines.extend(["", "## 7. 本次重新生成原因", "", reason])
    return "\n".join(lines)


def _ad_material_language_code(task):
    value = str(task.get("language") or "").strip().lower()
    return re.split(r"[^a-zA-Z]+", value, 1)[0] if value else ""


def _ad_material_is_finance_product(task):
    text = " ".join([
        str(task.get("product_name") or ""),
        str(task.get("description") or ""),
        str(task.get("title") or ""),
        str(task.get("body") or ""),
    ]).lower()
    tokens = (
        "cash", "loan", "credit", "credito", "crédito", "prestamo", "préstamo",
        "fintech", "dinero", "lend", "wallet", "pago", "pay",
    )
    return any(token in text for token in tokens)


def _ad_material_copy_variants(task, quantity):
    product = str(task.get("product_name") or task.get("app_id") or "Product").strip()
    title = str(task.get("title") or "").strip()
    body = str(task.get("body") or "").strip()
    lang = _ad_material_language_code(task)
    finance = _ad_material_is_finance_product(task)

    if title or body:
        cta_map = {"es": "Solicitar ahora", "pt": "Começar agora", "en": "Get started", "id": "Mulai sekarang"}
        cta = cta_map.get(lang, "立即体验" if lang in ("zh", "cn") else "Start now")
        return [{
            "headline": title or product,
            "body": body or "突出产品核心卖点，文案保持简短清晰。",
            "cta": cta,
        } for _ in range(quantity)]

    if lang == "es" and finance:
        base = [
            ("Préstamo rápido con %s" % product, "Solicita en línea desde tu celular.", "Solicitar ahora"),
            ("%s para tus planes" % product, "Proceso simple, claro y desde la app.", "Ver mi opción"),
            ("Crédito personal en pocos pasos", "Consulta tu monto disponible de forma sencilla.", "Empezar ahora"),
        ]
    elif lang == "es":
        base = [
            ("%s listo para usar" % product, "Empieza en pocos pasos desde tu celular.", "Probar ahora"),
            ("Descubre %s" % product, "Una experiencia simple, clara y práctica.", "Empezar ahora"),
            ("%s para tu día a día" % product, "Abre la app y continúa en segundos.", "Usar ahora"),
        ]
    elif lang == "pt" and finance:
        base = [
            ("Crédito rápido com %s" % product, "Solicite online pelo celular.", "Solicitar agora"),
            ("%s para seus planos" % product, "Processo simples e direto no app.", "Ver opção"),
            ("Crédito em poucos passos", "Confira sua opção disponível com clareza.", "Começar agora"),
        ]
    elif lang == "pt":
        base = [
            ("%s pronto para usar" % product, "Comece em poucos passos pelo celular.", "Começar agora"),
            ("Descubra %s" % product, "Uma experiência simples e prática.", "Usar agora"),
            ("%s no seu dia a dia" % product, "Abra o app e continue em segundos.", "Experimentar"),
        ]
    elif lang == "en" and finance:
        base = [
            ("Fast credit with %s" % product, "Apply online from your phone.", "Apply now"),
            ("%s for your plans" % product, "A simple app-first request flow.", "Check options"),
            ("Personal credit in a few steps", "Clear information before you continue.", "Get started"),
        ]
    elif lang == "en":
        base = [
            ("%s is ready to use" % product, "Start in a few simple steps from your phone.", "Try now"),
            ("Discover %s" % product, "A simple and practical app experience.", "Get started"),
            ("%s for everyday use" % product, "Open the app and continue in seconds.", "Use now"),
        ]
    else:
        base = [
            ("%s，立即体验" % product, "打开应用，按步骤完成操作。", "立即开始"),
            ("用 %s 解决当前需求" % product, "信息清晰、操作简单、移动端优先。", "立即体验"),
            ("%s，简单好用" % product, "突出核心利益点，减少干扰信息。", "马上使用"),
        ]

    return [
        {"headline": base[index % len(base)][0], "body": base[index % len(base)][1], "cta": base[index % len(base)][2]}
        for index in range(quantity)
    ]


def build_ad_material_image_generation_demand(task, reason=""):
    product = str(task.get("product_name") or task.get("app_id") or "未命名产品").strip()
    quantity = max(1, int(task.get("quantity") or 1))
    size = _ad_material_size(task)
    language = str(task.get("language") or "目标语言").strip()
    country = str(task.get("country") or "目标市场").strip()
    task_type = str(task.get("task_type") or "").strip()
    source = str(task.get("competitor_source") or "").strip()
    reference_items = _ad_material_reference_items(task)
    reference_names = [item["name"] for item in reference_items]
    has_refs = bool(reference_names)
    description = str(task.get("description") or "").strip()
    copy_variants = _ad_material_copy_variants(task, quantity)
    layouts = [
        "左文右图结构：左侧 45% 放主标题、副文案和 CTA，右侧 55% 放手机界面/产品核心视觉；logo 固定在左上角，底部留 8% 安全边距。",
        "上卖点下行动结构：顶部 20% 放 logo 和主标题，中部放产品界面或核心主体，底部用高对比按钮承载 CTA；画面中心保持单一视觉焦点。",
        "卡片式信息结构：背景干净，中央放一张大信息卡，卡内包含主标题、2 个利益点和 CTA；右下角可放手机 mockup 或产品使用场景。",
        "对角线视觉结构：左上为品牌与文案，右下为产品界面/主体，使用柔和色块引导视线；CTA 放在视觉终点，不遮挡主体。",
    ]

    lines = [
        "# %s AI生图素材需求" % product,
        "",
        "## 素材参考",
        "",
    ]
    if has_refs:
        lines.append("以下素材只作为 AI 生图的视觉参考，必须先识别画面主体、构图、色彩、文字层级、按钮样式和可迁移元素；不得直接复制原图。")
        lines.append("")
        for index, item in enumerate(reference_items, 1):
            name = item["name"]
            lines.append("- REF_%02d：%s；继承方向：版式节奏、信息层级、主体关系、色彩氛围；禁止直接照搬原图细节。" % (index, name))
            if item.get("url") and str(item.get("content_type") or "").lower().startswith("image/"):
                lines.append("")
                lines.append("![REF_%02d %s](%s)" % (index, name, item["url"]))
                lines.append("")
            elif item.get("url"):
                lines.append("  预览链接：%s" % item["url"])
    else:
        lines.append("- 暂无上传素材参考。AI 生图时不得假设已有参考图；若后续补充参考图，需优先按参考图识别结果调整构图、色彩和主体关系。")

    lines.extend(["", "## 竞品素材参考", ""])
    task_kind = ad_material_task_kind(task_type)
    if task_kind == "iteration":
        lines.append("- 本次任务不强制使用竞品素材参考；画面以「素材参考」和下方详细素材需求为准。")
    elif task_kind == "reference":
        lines.append("- 本次任务不强制使用竞品素材参考；画面以需求人上传的参考素材解析结果为主要风格依据。")
    else:
        source_text = source or "已配置竞品源"
        lines.append("- 竞品来源：%s。" % source_text)
        lines.append("- 只使用筛选后的 image-only 竞品静态图作为参考；只学习构图、卖点表达、CTA 位置、信息层级和色彩节奏。")
        lines.append("- 禁止复制竞品 logo、品牌色、人物/界面细节、不可验证承诺或任何容易造成品牌混淆的元素。")
        lines.append("- 若当前任务尚未绑定具体竞品图片，生图前需要补齐竞品图 URL/文件及视觉识别结论，不能凭空套用固定模板。")

    lines.extend(["", "## 详细素材需求", ""])
    lines.append("- 输出类型：静态图片素材，仅生成 jpg/png/webp 等图片，不包含视频脚本或投放策略。")
    lines.append("- 尺寸与数量计划：%s；共 %s 张；文案语言使用 %s；面向市场 %s。" % (size, quantity, language, country))
    lines.append("- 品牌规则：画面必须出现 %s 的 logo 或预留 logo 位；不得出现竞品品牌资产。" % product)
    lines.append("- 版式对齐硬约束：文案和背景容器必须严格对齐；主标题、副文案、信息卡、表格字段、按钮文字和免责声明必须完整落在对应白底/色块/卡片/表格/按钮内部，不得跨出边框、压线、悬浮在背景外或与装饰元素重叠；文字过长时必须缩短、换行、缩小字号或放大容器，禁止溢出。")
    if description:
        lines.append("- 用户补充方向：%s" % description)
    if reason:
        lines.append("- 本轮重做重点：%s" % reason)
    lines.append("")

    for index in range(1, quantity + 1):
        asset_size = ad_material_asset_size(task, index)
        copy_item = copy_variants[index - 1]
        ref_hint = "优先参考 REF_%02d" % (((index - 1) % len(reference_names)) + 1) if has_refs else "无上传参考图，按本条需求直接生成"
        layout = layouts[(index - 1) % len(layouts)]
        lines.extend([
            "### 素材 %02d" % index,
            "",
            "- 输出尺寸：%s" % asset_size,
            "- 主文案：\"%s\"" % copy_item["headline"],
            "- 副文案：\"%s\"" % copy_item["body"],
            "- CTA：\"%s\"" % copy_item["cta"],
            "- 布局：%s" % layout,
            "- 画面主体：以 %s 产品体验为核心，建议使用手机界面、产品核心功能卡片或用户使用场景作为主体；主体占画面 45%%-60%%，背景保持简洁。" % product,
            "- 文案排版：主文案最大、3 秒内可读；副文案不超过两行；CTA 做成清晰按钮；所有文案必须在对应卡片/表格/按钮/免责声明底色内部，不能越界、压线、贴边、漂浮或被图形遮挡。",
            "- 参考继承：%s；只继承可迁移的构图、色彩和信息层级，不复制原图/竞品的品牌资产。" % ref_hint,
            "- 禁止元素：夸大承诺、保证通过、官方背书、无审核、秒到账、竞品 logo、低清文字、乱码文字、过多小字、遮挡主体的装饰。",
            "- 验收标准：尺寸符合 %s；主文案、副文案、CTA 清晰无拼写错误；logo/预留 logo 位清楚；画面第一眼能理解产品卖点。" % asset_size,
            "",
        ])

    return "\n".join(lines).strip()


def ad_material_pdf_filename(task):
    name = re.sub(r"[^0-9A-Za-z_-]+", "_", str(task.get("product_name") or task.get("app_id") or "ad_material")).strip("_")
    return "%s_requirement_%s.pdf" % (name or "ad_material", task["task_id"][:8])


def ad_material_markdown_plain(text):
    import html as html_lib
    text = re.sub(r"<img[^>]*>", " ", str(text or ""), flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)
    text = re.sub(r"https?://\S+\.(?:png|jpe?g|webp|gif)(?:\?\S*)?", " ", text, flags=re.I)
    text = re.sub(r"https?://play-lh\.googleusercontent\.com/\S+", " ", text, flags=re.I)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    return html_lib.unescape(re.sub(r"\s+", " ", text).strip())


def ad_material_markdown_images(text):
    images = []
    for match in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>', str(text or ""), flags=re.I):
        images.append(match.group(1))
    for match in re.finditer(r"!\[[^\]]*\]\((https?://[^)\s]+|/[^)\s]+)\)", str(text or ""), flags=re.I):
        images.append(match.group(1))
    seen = set()
    result = []
    for url in images:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def ad_material_download_pdf_image(url, temp_dir):
    if not url or not str(url).startswith(("http://", "https://")):
        return ""
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        suffix = ".jpg"
        if "png" in content_type:
            suffix = ".png"
        elif "webp" in content_type:
            suffix = ".webp"
        path = os.path.join(temp_dir, hashlib.md5(url.encode("utf-8")).hexdigest() + suffix)
        with open(path, "wb") as handle:
            handle.write(resp.content)
        return path
    except Exception:
        logging.exception("failed to download pdf image: %s", url)
        return ""


def ad_material_add_pdf_image(story, url, temp_dir, max_width=160, max_height=130):
    try:
        from PIL import Image as PilImage
        from reportlab.platypus import Image as PdfImage

        path = ad_material_download_pdf_image(url, temp_dir)
        if not path:
            return False
        with PilImage.open(path) as image:
            width, height = image.size
        if width <= 0 or height <= 0:
            return False
        scale = min(float(max_width) / width, float(max_height) / height, 1.0)
        story.append(PdfImage(path, width=width * scale, height=height * scale))
        return True
    except Exception:
        logging.exception("failed to append pdf image: %s", url)
        return False


def ad_material_parse_markdown_table(lines, start_index):
    rows = []
    index = start_index
    while index < len(lines) and re.match(r"^\s*\|.+\|\s*$", lines[index]):
        cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
        if not all(re.match(r"^:?-{3,}:?$", cell) for cell in cells):
            rows.append(cells)
        index += 1
    return rows, index


def render_ad_material_demand_pdf_pillow(task, demand_text, artifacts=None):
    try:
        from PIL import Image as PilImage, ImageDraw, ImageFont
    except Exception as exc:
        raise StructuredApiError("pdf_dependency_missing", "服务端缺少 PDF 生成依赖：%s" % exc)

    public_dir = os.path.join(ad_material_public_dir(task["task_id"]), "exports")
    ensure_dir(public_dir)
    pdf_path = os.path.join(public_dir, ad_material_pdf_filename(task))
    temp_dir = tempfile.mkdtemp(prefix="ad-material-pdf-")
    width, height = 1240, 1754
    margin_x, margin_y = 72, 64
    max_text_width = width - margin_x * 2
    font_candidates = [
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]

    def font(size, bold=False):
        paths = font_candidates[:]
        if bold:
            paths.insert(0, "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc")
            paths.insert(1, "C:/Windows/Fonts/msyhbd.ttc")
        for path in paths:
            try:
                if os.path.exists(path):
                    return ImageFont.truetype(path, size=size)
            except Exception:
                continue
        return ImageFont.load_default()

    fonts = {
        "title": font(34, True),
        "h2": font(25, True),
        "h3": font(21, True),
        "body": font(18),
        "small": font(15),
    }
    pages = []
    image = None
    draw = None
    y = margin_y

    def text_width(text, fnt):
        try:
            bbox = draw.textbbox((0, 0), text, font=fnt)
            return bbox[2] - bbox[0]
        except Exception:
            return draw.textsize(text, font=fnt)[0]

    def line_height(fnt, extra=8):
        try:
            bbox = fnt.getbbox("国Ag")
            return (bbox[3] - bbox[1]) + extra
        except Exception:
            return 24 + extra

    def new_page():
        nonlocal image, draw, y
        image = PilImage.new("RGB", (width, height), "#FFFFFF")
        draw = ImageDraw.Draw(image)
        pages.append(image)
        y = margin_y

    def ensure_space(needed):
        if y + needed > height - margin_y:
            new_page()

    def draw_wrapped(text, fnt, fill="#172033", indent=0, spacing=6):
        nonlocal y
        text = ad_material_markdown_plain(text)
        if not text:
            return
        max_width = max_text_width - indent
        line = ""
        lines = []
        for char in text:
            candidate = line + char
            if line and text_width(candidate, fnt) > max_width:
                lines.append(line)
                line = char
            else:
                line = candidate
        if line:
            lines.append(line)
        lh = line_height(fnt)
        ensure_space(lh * max(1, len(lines)) + spacing)
        for line in lines:
            draw.text((margin_x + indent, y), line, font=fnt, fill=fill)
            y += lh
        y += spacing

    def draw_rule():
        nonlocal y
        ensure_space(20)
        draw.line((margin_x, y, width - margin_x, y), fill="#D8E2F0", width=2)
        y += 18

    def draw_image_from_url(url, max_w=300, max_h=210):
        nonlocal y
        path = ad_material_download_pdf_image(url, temp_dir)
        if not path:
            return False
        try:
            with PilImage.open(path) as raw:
                raw = raw.convert("RGB")
                raw.thumbnail((max_w, max_h))
                ensure_space(raw.height + 14)
                image.paste(raw, (margin_x, y))
                y += raw.height + 14
            return True
        except Exception:
            logging.exception("failed to draw pdf image: %s", url)
            return False

    def wrap_text_lines(text, fnt, max_width, max_lines=None):
        text = ad_material_markdown_plain(text)
        if not text:
            return []
        lines = []
        current = ""
        for char in text:
            candidate = current + char
            if current and text_width(candidate, fnt) > max_width:
                lines.append(current)
                current = char
                if max_lines and len(lines) >= max_lines:
                    break
            else:
                current = candidate
        if current and (not max_lines or len(lines) < max_lines):
            lines.append(current)
        if max_lines and len(lines) >= max_lines and len(ad_material_markdown_plain(text)) > len("".join(lines)):
            lines[-1] = lines[-1].rstrip("，。；,. ") + "..."
        return lines

    def draw_text_cell(text, x, top, cell_width, cell_height, fnt, fill="#172033", padding=12):
        lines = wrap_text_lines(text, fnt, cell_width - padding * 2, max(1, int((cell_height - padding * 2) / line_height(fnt, 4))))
        cursor = top + padding
        for text_line in lines:
            draw.text((x + padding, cursor), text_line, font=fnt, fill=fill)
            cursor += line_height(fnt, 4)

    def markdown_cell_image_url(cell):
        match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', cell or "", flags=re.I)
        if match:
            return match.group(1)
        match = re.search(r"!\[[^\]]*\]\((https?://[^)\s]+|/[^)\s]+)\)", cell or "", flags=re.I)
        if match:
            return match.group(1)
        return ""

    def draw_image_cell(url, x, top, cell_width, cell_height):
        path = ad_material_download_pdf_image(url, temp_dir)
        if not path:
            draw_text_cell("暂无预览", x, top, cell_width, cell_height, fonts["small"], "#8A96A8")
            return
        try:
            with PilImage.open(path) as raw:
                raw = raw.convert("RGB")
                raw.thumbnail((cell_width - 24, cell_height - 24))
                paste_x = int(x + (cell_width - raw.width) / 2)
                paste_y = int(top + (cell_height - raw.height) / 2)
                image.paste(raw, (paste_x, paste_y))
        except Exception:
            logging.exception("failed to draw pdf table image: %s", url)
            draw_text_cell("预览失败", x, top, cell_width, cell_height, fonts["small"], "#8A96A8")

    def draw_table(rows):
        nonlocal y
        if not rows:
            return
        data_rows = rows[1:] if len(rows) > 1 else rows
        if any(markdown_cell_image_url(cell) for row in data_rows for cell in row):
            col_widths = [120, 185, 530, max_text_width - 120 - 185 - 530]
            headers = rows[0] if rows and len(rows[0]) >= 4 else ["编号", "预览", "生图可参考点", "禁止照搬"]
            header_h = 46
            row_gap = 0

            def draw_header():
                nonlocal y
                x = margin_x
                draw.rectangle((margin_x, y, margin_x + max_text_width, y + header_h), fill="#F8FBFF", outline="#D8E2F0", width=1)
                for idx, col_width in enumerate(col_widths):
                    if idx:
                        draw.line((x, y, x, y + header_h), fill="#D8E2F0", width=1)
                    draw.text((x + 12, y + 13), ad_material_markdown_plain(headers[idx] if idx < len(headers) else ""), font=fonts["small"], fill="#102A56")
                    x += col_width
                y += header_h

            ensure_space(header_h + 220)
            draw_header()
            for row in data_rows[:24]:
                ref = row[0] if len(row) > 0 else ""
                image_url = next((markdown_cell_image_url(cell) for cell in row if markdown_cell_image_url(cell)), "")
                learn = row[2] if len(row) > 2 else ""
                forbidden = row[3] if len(row) > 3 else ""
                text_lines = max(
                    len(wrap_text_lines(learn, fonts["small"], col_widths[2] - 24, 18)),
                    len(wrap_text_lines(forbidden, fonts["small"], col_widths[3] - 24, 18)),
                    6,
                )
                row_h = min(520, max(220, text_lines * line_height(fonts["small"], 4) + 28))
                if y + row_h > height - margin_y:
                    new_page()
                    draw_header()
                x = margin_x
                draw.rectangle((margin_x, y, margin_x + max_text_width, y + row_h), fill="#FFFFFF", outline="#D8E2F0", width=1)
                for idx, col_width in enumerate(col_widths):
                    if idx:
                        draw.line((x, y, x, y + row_h), fill="#D8E2F0", width=1)
                    if idx == 0:
                        draw_text_cell(ref, x, y, col_width, row_h, fonts["small"], "#172033")
                    elif idx == 1:
                        draw_image_cell(image_url, x, y, col_width, row_h)
                    elif idx == 2:
                        draw_text_cell(learn, x, y, col_width, row_h, fonts["small"], "#172033")
                    else:
                        draw_text_cell(forbidden, x, y, col_width, row_h, fonts["small"], "#172033")
                    x += col_width
                y += row_h + row_gap
            y += 18
            return
        for row in rows[:18]:
            draw_wrapped(" | ".join(ad_material_markdown_plain(cell) for cell in row[:4]), fonts["small"], "#44546A")

    new_page()
    draw_wrapped("%s 投放素材需求" % (task.get("product_name") or task.get("app_id") or ""), fonts["title"], "#102A56")
    draw_wrapped("任务ID：%s    类型：%s    状态：%s    国家/语言：%s/%s    数量：%s" % (
        task.get("task_id", ""),
        task.get("task_type", ""),
        task.get("status_label") or task.get("status", ""),
        task.get("country", ""),
        task.get("language", ""),
        task.get("quantity", ""),
    ), fonts["small"], "#5D6B82")
    draw_rule()

    lines = str(demand_text or "").splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        line = raw.strip()
        if not line:
            index += 1
            continue
        if re.match(r"^\s*\|.+\|\s*$", raw):
            rows, index = ad_material_parse_markdown_table(lines, index)
            draw_table(rows)
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        if heading:
            level = len(heading.group(1))
            if level <= 2:
                draw_rule()
            draw_wrapped(heading.group(2), fonts["h2"] if level <= 2 else fonts["h3"], "#174EA6" if level <= 2 else "#172033")
            index += 1
            continue
        images = ad_material_markdown_images(line)
        if images and len(ad_material_markdown_plain(line)) < 20:
            for url in images[:3]:
                draw_image_from_url(url, max_w=440, max_h=320)
            index += 1
            continue
        bullet_match = re.match(r"^[-*]\s+(.+)$", line)
        if bullet_match:
            draw_wrapped("• " + bullet_match.group(1), fonts["body"], "#172033", indent=16)
        else:
            draw_wrapped(line, fonts["body"], "#172033")
        index += 1

    try:
        pages[0].save(pdf_path, "PDF", resolution=150.0, save_all=True, append_images=pages[1:])
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return {"pdf_path": pdf_path, "pdf_url": build_public_url(pdf_path)}


def render_ad_material_demand_pdf(task, demand_text, artifacts=None):
    if not str(demand_text or "").strip():
        raise StructuredApiError("empty_demand", "暂无需求内容，无法导出 PDF")
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except Exception as exc:
        logging.info("reportlab unavailable, falling back to pillow pdf renderer: %s", exc)
        return render_ad_material_demand_pdf_pillow(task, demand_text, artifacts)

    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        font_name = "STSong-Light"
    except Exception:
        font_name = "Helvetica"

    public_dir = os.path.join(ad_material_public_dir(task["task_id"]), "exports")
    ensure_dir(public_dir)
    pdf_path = os.path.join(public_dir, ad_material_pdf_filename(task))
    temp_dir = tempfile.mkdtemp(prefix="ad-material-pdf-")
    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "AdMaterialBase",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=9.5,
        leading=14,
        textColor=colors.HexColor("#172033"),
        alignment=TA_LEFT,
        spaceAfter=5,
    )
    title = ParagraphStyle("AdMaterialTitle", parent=base, fontSize=18, leading=24, textColor=colors.HexColor("#102A56"), spaceAfter=12)
    h2 = ParagraphStyle("AdMaterialH2", parent=base, fontSize=13, leading=18, textColor=colors.HexColor("#174EA6"), spaceBefore=10, spaceAfter=7)
    h3 = ParagraphStyle("AdMaterialH3", parent=base, fontSize=11.5, leading=16, textColor=colors.HexColor("#172033"), spaceBefore=7, spaceAfter=5)
    bullet = ParagraphStyle("AdMaterialBullet", parent=base, leftIndent=12, firstLineIndent=-8)
    small = ParagraphStyle("AdMaterialSmall", parent=base, fontSize=8, leading=11, textColor=colors.HexColor("#5D6B82"))

    def pdf_text(value):
        import html as html_lib
        return html_lib.escape(ad_material_markdown_plain(value))

    def para(value, style=base):
        return Paragraph(pdf_text(value), style)

    story = [
        Paragraph(pdf_text("%s 投放素材需求" % (task.get("product_name") or task.get("app_id") or "")), title),
        Paragraph(pdf_text("任务ID：%s    类型：%s    状态：%s    国家/语言：%s/%s    数量：%s" % (
            task.get("task_id", ""),
            task.get("task_type", ""),
            task.get("status_label") or task.get("status", ""),
            task.get("country", ""),
            task.get("language", ""),
            task.get("quantity", ""),
        )), small),
        Spacer(1, 6),
    ]

    lines = str(demand_text or "").splitlines()
    index = 0
    table_count = 0
    while index < len(lines):
        raw = lines[index]
        line = raw.strip()
        if not line:
            index += 1
            continue
        if re.match(r"^\s*\|.+\|\s*$", raw):
            rows, index = ad_material_parse_markdown_table(lines, index)
            if rows:
                table_count += 1
                header = rows[0]
                data_rows = rows[1:] if len(rows) > 1 else []
                if any("img" in cell.lower() for row in data_rows for cell in row):
                    story.append(Paragraph("参考素材", h2 if table_count == 1 else h3))
                    for row in data_rows[:24]:
                        ref = row[0] if row else ""
                        image_url = ""
                        for cell in row:
                            match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', cell, flags=re.I)
                            if match:
                                image_url = match.group(1)
                                break
                        text_cells = [ad_material_markdown_plain(cell) for cell in row[2:] if ad_material_markdown_plain(cell)]
                        card = [Paragraph("<b>%s</b>" % pdf_text(ref), h3)]
                        if image_url:
                            ad_material_add_pdf_image(card, image_url, temp_dir, max_width=150, max_height=120)
                        if text_cells:
                            card.append(Paragraph(pdf_text("; ".join(text_cells)[:1400]), small))
                        story.append(KeepTogether(card))
                        story.append(Spacer(1, 6))
                else:
                    table_data = [[Paragraph(pdf_text(cell)[:260], small) for cell in row[:4]] for row in rows[:18]]
                    table = Table(table_data, hAlign="LEFT", repeatRows=1)
                    table.setStyle(TableStyle([
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF4FF")),
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8E2F0")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 5),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ]))
                    story.append(table)
                    story.append(Spacer(1, 8))
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        if heading:
            level = len(heading.group(1))
            story.append(Paragraph(pdf_text(heading.group(2)), title if level == 1 else h2 if level == 2 else h3))
            index += 1
            continue
        images = ad_material_markdown_images(line)
        if images and len(ad_material_markdown_plain(line)) < 20:
            for url in images[:3]:
                ad_material_add_pdf_image(story, url, temp_dir, max_width=260, max_height=180)
            index += 1
            continue
        bullet_match = re.match(r"^[-*]\s+(.+)$", line)
        if bullet_match:
            story.append(Paragraph("• " + pdf_text(bullet_match.group(1)), bullet))
        else:
            story.append(para(line, base))
        if len(story) % 85 == 0:
            story.append(PageBreak())
        index += 1

    def page_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 8)
        canvas.setFillColor(colors.HexColor("#7B8798"))
        canvas.drawString(18 * mm, 11 * mm, "AI 自动后台 | 投放素材需求")
        canvas.drawRightString(A4[0] - 18 * mm, 11 * mm, "Page %s" % doc.page)
        canvas.restoreState()

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title="%s 投放素材需求" % (task.get("product_name") or task.get("app_id") or ""),
    )
    try:
        doc.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return {"pdf_path": pdf_path, "pdf_url": build_public_url(pdf_path)}


def ensure_ad_material_demand_pdf(task, demand_text=None, artifacts=None):
    artifacts = dict(artifacts or task.get("demand_artifacts") or {})
    if artifacts.get("pdf_url"):
        return artifacts
    pdf_artifacts = render_ad_material_demand_pdf(task, demand_text if demand_text is not None else task.get("demand_text", ""), artifacts)
    artifacts.update(pdf_artifacts)
    return artifacts


def export_ad_material_demand_pdf(task_id, session):
    task = fetch_ad_material_task(task_id)
    ensure_ad_material_access(session, task)
    artifacts = ensure_ad_material_demand_pdf(task)
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                "UPDATE ad_material_task SET demand_artifacts_json=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                (json.dumps(artifacts, ensure_ascii=False), task_id),
            )
            conn.commit()
        finally:
            conn.close()
    updated = fetch_ad_material_task(task_id)
    return {"task_id": task_id, "pdf_url": artifacts.get("pdf_url", ""), "task": updated}


def notify_ad_material_task_owner(task, text):
    try:
        if task.get("creator_open_id"):
            message = str(text or "").strip()
            admin_url = AD_MATERIAL_ADMIN_URL.rstrip("/")
            if admin_url and admin_url not in message:
                message = "%s\nAI后台：%s" % (message, admin_url)
            send_feishu_text("open_id", task["creator_open_id"], message)
    except Exception:
        logging.exception("failed to notify ad material owner: %s", task.get("task_id"))


def ad_material_competitor_alert_recipients(task=None):
    recipients = []
    if AD_MATERIAL_COMPETITOR_ALERT_RECEIVE_ID_TYPE and AD_MATERIAL_COMPETITOR_ALERT_RECEIVE_ID:
        recipients.append((AD_MATERIAL_COMPETITOR_ALERT_RECEIVE_ID_TYPE, AD_MATERIAL_COMPETITOR_ALERT_RECEIVE_ID))
    for open_id in AD_MATERIAL_COMPETITOR_ALERT_OPEN_IDS:
        recipients.append(("open_id", open_id))
    try:
        with JOB_DB_LOCK:
            conn = get_job_db_connection()
            try:
                rows = conn.execute(
                    "SELECT open_id FROM drama_admin_user WHERE role = 'admin' AND TRIM(open_id) <> ''"
                ).fetchall()
            finally:
                conn.close()
        for row in rows:
            recipients.append(("open_id", str(row["open_id"] or "").strip()))
    except Exception:
        logging.exception("failed to load ad material competitor alert admin recipients")
    if task and task.get("creator_open_id"):
        recipients.append(("open_id", str(task.get("creator_open_id") or "").strip()))
    result = []
    seen = set()
    for receive_id_type, receive_id in recipients:
        key = (str(receive_id_type or "").strip(), str(receive_id or "").strip())
        if key[0] and key[1] and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def notify_ad_material_competitor_source_disabled(source, error, task=None):
    task = task or {}
    text = (
        "投放素材竞品源已自动临时下架\n"
        "竞品源：%s\n"
        "触发任务：%s / %s\n"
        "错误：%s\n"
        "处理：后台创建任务时将不再展示该竞品源，恢复前请改用其他来源。"
    ) % (
        source,
        task.get("task_id", ""),
        task.get("product_name") or task.get("app_id") or "",
        str(error or "")[:800],
    )
    recipients = ad_material_competitor_alert_recipients(task)
    if not recipients:
        logging.warning("ad material competitor source disabled without alert recipient: %s %s", source, error)
        return
    for receive_id_type, receive_id in recipients:
        try:
            send_feishu_text(receive_id_type, receive_id, text)
        except Exception:
            logging.exception("failed to send competitor source disabled alert: %s %s", receive_id_type, receive_id)


def disable_ad_material_competitor_source(source, error, task=None):
    source = str(source or "").strip()
    if source not in AD_MATERIAL_COMPETITOR_SOURCES:
        return
    error_text = str(error or "").strip()[:1000]
    previous_status = ""
    try:
        with JOB_DB_LOCK:
            conn = get_job_db_connection()
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ad_material_competitor_source (
                      source, status, fail_count, last_error, disabled_at, created_at, updated_at
                    ) VALUES (?, 'active', 0, '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (source,),
                )
                row = conn.execute(
                    "SELECT status FROM ad_material_competitor_source WHERE source = ?",
                    (source,),
                ).fetchone()
                previous_status = str(row["status"] or "") if row else ""
                conn.execute(
                    """
                    UPDATE ad_material_competitor_source
                    SET status = 'disabled',
                        fail_count = fail_count + 1,
                        last_error = ?,
                        disabled_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE source = ?
                    """,
                    (error_text, source),
                )
                conn.commit()
            finally:
                conn.close()
        if previous_status != "disabled":
            notify_ad_material_competitor_source_disabled(source, error_text, task)
    except Exception:
        logging.exception("failed to disable ad material competitor source: %s", source)


def generate_ad_material_demand(task_id, reason=""):
    task = fetch_ad_material_task(task_id)
    if not task:
        return
    update_ad_material_task_status(task_id, "generating_demand", review_reason=reason, error_message="")
    try:
        result = run_ad_material_external_command(AD_MATERIAL_REQUIREMENT_COMMAND, task, "demand", {"reason": reason}) if AD_MATERIAL_REQUIREMENT_COMMAND else {}
        demand_text = str(result.get("demand_text") or result.get("markdown") or "").strip()
        demand_artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
        if not demand_text:
            demand_text = build_ad_material_image_generation_demand(task, reason)
        try:
            demand_artifacts = ensure_ad_material_demand_pdf(task, demand_text, demand_artifacts)
        except Exception as pdf_exc:
            logging.exception("ad material demand pdf generation failed: %s", task_id)
            demand_artifacts["pdf_error"] = str(pdf_exc)
        update_ad_material_task_status(
            task_id,
            "demand_review",
            demand_text=demand_text,
            demand_artifacts_json=json.dumps(demand_artifacts, ensure_ascii=False),
            error_message="",
        )
        fresh = fetch_ad_material_task(task_id)
        notify_ad_material_task_owner(fresh, "投放素材任务需求已生成，请审核：%s" % (fresh.get("product_name") or fresh.get("task_id")))
    except Exception as exc:
        logging.exception("ad material demand generation failed: %s", task_id)
        if task.get("competitor_source") and ad_material_task_kind(task.get("task_type")) not in ("iteration", "reference"):
            disable_ad_material_competitor_source(task.get("competitor_source"), exc, task)
        update_ad_material_task_status(task_id, "failed", error_message=str(exc))


def run_ad_material_demand_async(task_id, reason=""):
    thread = threading.Thread(target=generate_ad_material_demand, args=(task_id, reason), name="ad-demand-%s" % task_id[:8])
    thread.daemon = True
    thread.start()


def write_placeholder_ad_material_asset(task, index):
    public_dir = ad_material_public_dir(task["task_id"])
    ensure_dir(public_dir)
    asset_id = "%s_%02d" % (task["task_id"], index)
    filename = "%s.svg" % asset_id
    path = os.path.join(public_dir, filename)
    title = "%s #%02d" % (task.get("product_name") or task.get("app_id"), index)
    width_text, height_text = ad_material_asset_output_size(task, index).split("x", 1)
    width = int(width_text)
    height = int(height_text)
    center_x = width // 2
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%s" viewBox="0 0 %s %s">
<rect width="%s" height="%s" fill="#eef4ff"/>
<rect x="%s" y="%s" width="%s" height="%s" rx="36" fill="#ffffff" stroke="#2f6bff" stroke-width="8"/>
<text x="%s" y="%s" font-family="Arial,sans-serif" font-size="54" font-weight="700" text-anchor="middle" fill="#172033">%s</text>
<text x="%s" y="%s" font-family="Arial,sans-serif" font-size="34" text-anchor="middle" fill="#44546a">%s / %s / %s</text>
<text x="%s" y="%s" font-family="Arial,sans-serif" font-size="30" text-anchor="middle" fill="#667085">待接入真实AI/GPU生成服务</text>
</svg>""" % (
        width,
        height,
        width,
        height,
        width,
        height,
        max(40, width // 12),
        max(60, height // 9),
        max(120, width - width // 6),
        max(200, height - height // 5),
        center_x,
        height * 36 // 100,
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
        center_x,
        height * 48 // 100,
        task.get("task_type", ""),
        task.get("country", ""),
        task.get("language", ""),
        center_x,
        height * 58 // 100,
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(svg)
    return {
        "asset_id": asset_id,
        "task_id": task["task_id"],
        "asset_index": index,
        "name": "%s_%02d" % (task.get("product_name") or "ad_material", index),
        "url": publish_asset(path),
        "local_path": path,
        "status": "pending_review",
        "review_reason": "",
        "source_api_id": "",
        "source_api_error": "",
    }


def generation_outputs_to_assets(task, result, indexes=None):
    outputs = result.get("outputs") or result.get("assets") or []
    assets = []
    wanted = set(indexes or [])
    for offset, item in enumerate(outputs, 1):
        if not isinstance(item, dict):
            continue
        index = int(item.get("asset_index") or item.get("index") or offset)
        if wanted and index not in wanted:
            continue
        url = str(item.get("cos_url") or item.get("url") or "").strip()
        local_path = str(item.get("local_path") or item.get("path") or "").strip()
        if not url and local_path and file_ready(local_path):
            url = publish_asset(local_path)
        if not url:
            continue
        asset_id = str(item.get("asset_id") or "%s_%02d" % (task["task_id"], index))
        assets.append({
            "asset_id": asset_id,
            "task_id": task["task_id"],
            "asset_index": index,
            "name": str(item.get("name") or item.get("headline") or "%s_%02d" % (task.get("product_name") or "ad_material", index)),
            "url": url,
            "local_path": local_path,
            "status": "pending_review",
            "review_reason": "",
            "source_api_id": "",
            "source_api_error": "",
        })
    return assets


def generate_ad_material_assets(task_id, indexes=None, reason=""):
    task = fetch_ad_material_task(task_id)
    if not task:
        return
    partial_generation = indexes is not None
    quantity = int(task.get("quantity") or 1)
    raw_indexes = indexes if indexes is not None else list(range(1, quantity + 1))
    target_indexes = []
    for raw_index in raw_indexes:
        index = int(raw_index)
        if index not in target_indexes:
            target_indexes.append(index)
    if not target_indexes:
        update_ad_material_task_status(
            task_id,
            "material_review" if partial_generation else "failed",
            error_message="no ad material asset indexes requested",
        )
        return
    update_ad_material_task_status(
        task_id,
        "material_review" if partial_generation else "generating_material",
        review_reason=reason,
        error_message="",
    )
    try:
        assets = []
        if AD_MATERIAL_GENERATION_COMMAND:
            for index in target_indexes:
                task_for_index = dict(task)
                task_for_index["size_ratio"] = ad_material_asset_size(task, index)
                task_for_index["size"] = ad_material_asset_output_size(task, index)
                result = run_ad_material_external_command(
                    AD_MATERIAL_GENERATION_COMMAND,
                    task_for_index,
                    "generation_%02d" % index,
                    {"indexes": [index], "reason": reason, "size": task_for_index["size_ratio"]},
                )
                generated_assets = generation_outputs_to_assets(task, result, indexes=[index])
                if not generated_assets:
                    raise RuntimeError("generation command returned no downloadable asset for index %02d" % index)
                for asset in generated_assets:
                    upsert_ad_material_asset(asset)
                    assets.append(asset)
        else:
            assets = [write_placeholder_ad_material_asset(task, index) for index in target_indexes]
            for asset in assets:
                upsert_ad_material_asset(asset)
        update_ad_material_task_status(task_id, "material_review", error_message="")
        fresh = fetch_ad_material_task(task_id)
        notify_ad_material_task_owner(fresh, "投放素材已生成，请审核：%s" % (fresh.get("product_name") or fresh.get("task_id")))
    except Exception as exc:
        logging.exception("ad material generation failed: %s", task_id)
        update_ad_material_task_status(
            task_id,
            "material_review" if partial_generation else "failed",
            error_message=str(exc),
        )


def run_ad_material_generation_async(task_id, indexes=None, reason=""):
    thread = threading.Thread(target=generate_ad_material_assets, args=(task_id, indexes, reason), name="ad-assets-%s" % task_id[:8])
    thread.daemon = True
    thread.start()


def reconcile_ad_material_task_after_asset_review(task_id):
    assets = fetch_ad_material_assets(task_id)
    if not assets:
        return
    terminal_statuses = {"approved", "uploaded", "abandoned"}
    if not all(str(asset.get("status") or "").strip() in terminal_statuses for asset in assets):
        return
    if any(str(asset.get("status") or "").strip() in ("approved", "uploaded") for asset in assets):
        return
    update_ad_material_task_status(
        task_id,
        "material_abandoned",
        review_reason="all generated ad material assets were abandoned",
        error_message="",
    )


def ad_material_ready_asset_indexes(task):
    ready = set()
    for asset in fetch_ad_material_assets(task["task_id"]):
        try:
            index = int(asset.get("asset_index") or 0)
        except Exception:
            index = 0
        if index <= 0:
            continue
        status = str(asset.get("status") or "").strip()
        if status not in ("pending_review", "approved", "uploaded"):
            continue
        url = str(asset.get("url") or "").strip()
        local_path = str(asset.get("local_path") or "").strip()
        if url or (local_path and file_ready(local_path)):
            ready.add(index)
    return ready


def ad_material_generation_file_mtime(path):
    try:
        return datetime.utcfromtimestamp(os.path.getmtime(path))
    except OSError:
        return None


def ad_material_output_is_current(task_id, index, min_output_at=""):
    workdir = ad_material_task_work_dir(task_id)
    input_path = os.path.join(workdir, "generation_%02d_input.json" % index)
    output_path = os.path.join(workdir, "generation_%02d_output.json" % index)
    output_mtime = ad_material_generation_file_mtime(output_path)
    if not output_mtime:
        return False
    checkpoints = []
    input_mtime = ad_material_generation_file_mtime(input_path)
    if input_mtime:
        checkpoints.append(("input", input_mtime))
    min_dt = parse_job_timestamp(min_output_at)
    if min_dt:
        checkpoints.append(("asset", min_dt))
    stale_after = [(name, dt) for name, dt in checkpoints if output_mtime < dt]
    if stale_after:
        logging.info(
            "ignoring stale ad material generation output: task=%s index=%s output_mtime=%s checkpoints=%s",
            task_id,
            index,
            output_mtime.strftime("%Y-%m-%d %H:%M:%S"),
            ",".join("%s:%s" % (name, dt.strftime("%Y-%m-%d %H:%M:%S")) for name, dt in stale_after),
        )
        return False
    return True


def ad_material_regenerating_asset_targets(task_id):
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT asset_index, review_reason, updated_at
                FROM ad_material_asset
                WHERE task_id=? AND status='regenerating'
                ORDER BY asset_index, id
                """,
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
    targets = []
    seen = set()
    for row in rows:
        try:
            index = int(row["asset_index"] or 0)
        except Exception:
            index = 0
        if index <= 0 or index in seen:
            continue
        seen.add(index)
        targets.append({
            "index": index,
            "reason": str(row["review_reason"] or "").strip(),
            "updated_at": str(row["updated_at"] or "").strip(),
        })
    return targets


def recover_ad_material_generation_output(task, index, min_output_at=""):
    output_path = os.path.join(ad_material_task_work_dir(task["task_id"]), "generation_%02d_output.json" % index)
    if not os.path.isfile(output_path):
        return False
    if not ad_material_output_is_current(task["task_id"], index, min_output_at=min_output_at):
        return False
    try:
        with open(output_path, "r", encoding="utf-8") as handle:
            result = json.load(handle)
        assets = generation_outputs_to_assets(task, result, indexes=[index])
        if not assets:
            return False
        for asset in assets:
            upsert_ad_material_asset(asset)
        logging.info(
            "recovered ad material asset from existing generation output: task=%s index=%s",
            task.get("task_id"),
            index,
        )
        return True
    except Exception:
        logging.exception(
            "failed to recover ad material asset from generation output: task=%s index=%s",
            task.get("task_id"),
            index,
        )
        return False


def recover_inflight_ad_material_tasks():
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT task_id, status
                FROM ad_material_task
                WHERE status IN ('generating_demand', 'generating_material')
                ORDER BY updated_at ASC
                """
            ).fetchall()
            task_states = [(row["task_id"], row["status"]) for row in rows]
            regenerating_rows = conn.execute(
                """
                SELECT DISTINCT t.task_id, t.status
                FROM ad_material_task t
                JOIN ad_material_asset a ON a.task_id = t.task_id
                WHERE t.status='material_review' AND a.status='regenerating'
                ORDER BY t.updated_at ASC
                """
            ).fetchall()
            known_task_ids = {task_id for task_id, _ in task_states}
            for row in regenerating_rows:
                if row["task_id"] not in known_task_ids:
                    task_states.append((row["task_id"], "regenerating_assets"))
                    known_task_ids.add(row["task_id"])
        finally:
            conn.close()

    for task_id, status in task_states:
        try:
            task = fetch_ad_material_task(task_id)
        except Exception:
            logging.exception("skipping ad material restart recovery for unreadable task: %s", task_id)
            continue
        if not task:
            continue
        if status == "generating_demand":
            reason = str(task.get("review_reason") or "").strip()
            if reason.lower().startswith("service restart recovery"):
                reason = ""
            logging.info("resuming ad material demand after service restart: %s", task_id)
            run_ad_material_demand_async(task_id, reason=reason)
            continue

        if status == "regenerating_assets":
            targets = ad_material_regenerating_asset_targets(task_id)
            missing_indexes = []
            recovered_indexes = []
            reasons = []
            for target in targets:
                index = target["index"]
                reason = target["reason"]
                if reason:
                    reasons.append(reason)
                if recover_ad_material_generation_output(task, index, min_output_at=target["updated_at"]):
                    recovered_indexes.append(index)
                else:
                    missing_indexes.append(index)
            if missing_indexes:
                reason = "service restart recovery: regenerate interrupted ad material assets %s" % ",".join(
                    str(index) for index in missing_indexes
                )
                if reasons:
                    reason = "%s; original review reason: %s" % (reason, "; ".join(dict.fromkeys(reasons)))
                logging.info(
                    "resuming interrupted ad material asset regeneration after service restart: task=%s missing=%s recovered=%s",
                    task_id,
                    missing_indexes,
                    sorted(recovered_indexes),
                )
                update_ad_material_task_status(task_id, "material_review", review_reason=reason, error_message="")
                run_ad_material_generation_async(task_id, indexes=missing_indexes, reason=reason)
            elif recovered_indexes:
                logging.info(
                    "recovered interrupted ad material asset regeneration after service restart: task=%s recovered=%s",
                    task_id,
                    sorted(recovered_indexes),
                )
                update_ad_material_task_status(task_id, "material_review", error_message="")
            continue

        quantity = max(1, int(task.get("quantity") or 1))
        expected_indexes = list(range(1, quantity + 1))
        ready_indexes = ad_material_ready_asset_indexes(task)
        for index in expected_indexes:
            if index not in ready_indexes and recover_ad_material_generation_output(task, index):
                ready_indexes.add(index)

        missing_indexes = [index for index in expected_indexes if index not in ready_indexes]
        if not missing_indexes:
            logging.info("recovered ad material task from existing assets after service restart: %s", task_id)
            update_ad_material_task_status(task_id, "material_review", error_message="")
            continue

        reason = "service restart recovery: regenerate missing ad material assets %s" % ",".join(
            str(index) for index in missing_indexes
        )
        logging.info(
            "resuming ad material generation after service restart: task=%s missing=%s ready=%s",
            task_id,
            missing_indexes,
            sorted(ready_indexes),
        )
        if len(missing_indexes) == quantity:
            run_ad_material_generation_async(task_id, reason=reason)
        else:
            update_ad_material_task_status(task_id, "material_review", review_reason=reason, error_message="")
            run_ad_material_generation_async(task_id, indexes=missing_indexes, reason=reason)


def publish_ad_material_task(task_id, session):
    task = fetch_ad_material_task(task_id)
    ensure_ad_material_access(session, task)
    if task["status"] != "draft":
        raise StructuredApiError("invalid_status", "只有待发布任务可以发布")
    run_ad_material_demand_async(task_id)
    return fetch_ad_material_task(task_id)


def review_ad_material_demand(task_id, payload, session):
    task = fetch_ad_material_task(task_id)
    ensure_ad_material_access(session, task)
    if task["status"] != "demand_review":
        raise StructuredApiError("invalid_status", "当前状态不能审核需求")
    result = str(payload.get("result", "") or "").strip()
    reason = str(payload.get("reason", "") or "").strip()
    if result == "approved":
        run_ad_material_generation_async(task_id)
    elif result == "rejected":
        if not reason:
            raise StructuredApiError("reason_required", "驳回原因必填")
        update_ad_material_task_status(task_id, "demand_returned", review_reason=reason)
        run_ad_material_demand_async(task_id, reason=reason)
    else:
        raise StructuredApiError("invalid_review_result", "审核结果无效")
    return fetch_ad_material_task(task_id)


def review_ad_material_asset(task_id, asset_id, payload, session):
    task = fetch_ad_material_task(task_id)
    ensure_ad_material_access(session, task)
    result = str(payload.get("result", "") or "").strip()
    reason = str(payload.get("reason", "") or "").strip()
    assets = fetch_ad_material_assets(task_id)
    asset = next((item for item in assets if item["asset_id"] == asset_id), None)
    if not asset:
        raise StructuredApiError("not_found", "素材不存在")
    if result == "approved":
        status = "approved"
    elif result == "rejected":
        if not reason:
            raise StructuredApiError("reason_required", "驳回原因必填")
        status = "regenerating"
    elif result == "abandoned":
        status = "abandoned"
    else:
        raise StructuredApiError("invalid_review_result", "审核结果无效")
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                "UPDATE ad_material_asset SET status=?, review_reason=?, updated_at=CURRENT_TIMESTAMP WHERE asset_id=?",
                (status, reason, asset_id),
            )
            conn.commit()
        finally:
            conn.close()
    if status == "regenerating":
        update_ad_material_task_status(task_id, "material_review", review_reason=reason)
        run_ad_material_generation_async(task_id, indexes=[int(asset.get("asset_index") or 1)], reason=reason)
    elif status in ("approved", "abandoned"):
        reconcile_ad_material_task_after_asset_review(task_id)
    return fetch_ad_material_task(task_id)


def copy_ad_material_task(task_id, session):
    task = fetch_ad_material_task(task_id)
    ensure_ad_material_access(session, task)
    payload = dict(task)
    payload["reference_files"] = []
    copied = create_ad_material_task(payload, session)
    return copied


def delete_ad_material_task(task_id, session):
    task = fetch_ad_material_task(task_id)
    ensure_ad_material_access(session, task)
    if task["status"] == "done":
        raise StructuredApiError("task_done", "已完成任务不允许删除")
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute("DELETE FROM ad_material_asset WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM ad_material_task WHERE task_id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()
    shutil.rmtree(ad_material_task_work_dir(task_id), ignore_errors=True)
    shutil.rmtree(ad_material_public_dir(task_id), ignore_errors=True)
    return {"message": "deleted", "task_id": task_id}


def post_ad_material_source(task, asset):
    if not AD_MATERIAL_SOURCE_API_TOKEN:
        raise StructuredApiError("source_api_token_missing", "最终素材上报 token 未配置")
    if not task.get("initiator_sub_user_id"):
        admin_group = lookup_admin_group_by_email(task.get("creator_email")) or lookup_admin_group_by_name(task.get("creator_name"))
        if admin_group.get("sub_user_id"):
            with JOB_DB_LOCK:
                conn = get_job_db_connection()
                try:
                    conn.execute(
                        "UPDATE ad_material_task SET initiator_sub_user_id=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                        (admin_group["sub_user_id"], task["task_id"]),
                    )
                    conn.commit()
                finally:
                    conn.close()
            task["initiator_sub_user_id"] = admin_group["sub_user_id"]
    if not task.get("initiator_sub_user_id"):
        raise StructuredApiError("initiator_missing", "无法通过邮箱定位发起人 sub_user_id")
    body = {
        "app_id": int(task["app_id"]),
        "country": task["country"],
        "language": task["language"],
        "content_sign": asset["asset_id"],
        "url": asset["url"],
        "name": asset["name"] or asset["asset_id"],
        "user_id": AD_MATERIAL_FINAL_USER_ID,
        "initiator": int(task["initiator_sub_user_id"]),
        "category": task.get("category", ""),
        "tag_name": task.get("tag_name", ""),
        "title": task.get("title", ""),
        "body": task.get("body", ""),
        "remark": "",
    }
    response = requests.post(
        AD_MATERIAL_SOURCE_API_URL,
        headers={
            "Authorization": "Bearer %s" % AD_MATERIAL_SOURCE_API_TOKEN,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=AD_MATERIAL_SOURCE_API_TIMEOUT,
    )
    if response.status_code == 403:
        raise StructuredApiError("source_api_forbidden", "最终素材上报认证失败")
    data = response.json()
    code = data.get("code")
    if code not in (None, 0, "0", "success") and not data.get("success"):
        raise StructuredApiError("source_api_failed", str(data.get("message") or data.get("error") or data))
    source_id = ""
    if isinstance(data.get("data"), dict):
        source_id = str(data["data"].get("id") or "")
    return source_id or str(data.get("id") or "")


def complete_ad_material_upload(task_id, session):
    task = fetch_ad_material_task(task_id)
    ensure_ad_material_access(session, task)
    assets = fetch_ad_material_assets(task_id)
    if not assets:
        raise StructuredApiError("no_assets", "没有可上报素材")
    not_ready = [item for item in assets if item.get("status") not in ("approved", "uploaded", "abandoned")]
    if not_ready:
        raise StructuredApiError("asset_not_approved", "所有待上传素材审核通过后才能上报")
    uploadable_assets = [item for item in assets if item.get("status") in ("approved", "uploaded")]
    if not uploadable_assets:
        raise StructuredApiError("no_uploadable_assets", "没有可上传至素材库的素材")
    errors = []
    for asset in uploadable_assets:
        if asset.get("status") == "uploaded" and asset.get("source_api_id"):
            continue
        try:
            source_id = post_ad_material_source(task, asset)
            with JOB_DB_LOCK:
                conn = get_job_db_connection()
                try:
                    conn.execute(
                        "UPDATE ad_material_asset SET status='uploaded', source_api_id=?, source_api_error='', updated_at=CURRENT_TIMESTAMP WHERE asset_id=?",
                        (source_id, asset["asset_id"]),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as exc:
            errors.append({"asset_id": asset["asset_id"], "error": str(exc)})
            with JOB_DB_LOCK:
                conn = get_job_db_connection()
                try:
                    conn.execute(
                        "UPDATE ad_material_asset SET status='upload_failed', source_api_error=?, updated_at=CURRENT_TIMESTAMP WHERE asset_id=?",
                        (str(exc), asset["asset_id"]),
                    )
                    conn.commit()
                finally:
                    conn.close()
    if errors:
        update_ad_material_task_status(task_id, "material_review", error_message=json.dumps(errors, ensure_ascii=False))
        raise StructuredApiError("source_upload_failed", "部分素材上报失败", errors=errors)
    update_ad_material_task_status(task_id, "done", error_message="")
    return fetch_ad_material_task(task_id)


from features.voiceover_drama_tasks.service import (
    configure_voiceover_drama_tasks,
    create_voiceover_design_tasks,
    list_voiceover_designers,
    voiceover_filter_materials,
    voiceover_material_counts,
)

configure_voiceover_drama_tasks(
    ADMIN_MAPPING_MYSQL_DATABASE=ADMIN_MAPPING_MYSQL_DATABASE,
    DB_NAME=DB_NAME,
    StructuredApiError=StructuredApiError,
    run_mysql=run_mysql,
    mysql_escape_literal=mysql_escape_literal,
    app_package_for_app_id=app_package_for_app_id,
    ad_material_actor=ad_material_actor,
    api_error_payload=api_error_payload,
)



def parse_ad_material_task_route(path):
    match = re.match(r"^/api/ad-material/tasks/([0-9a-f]{32})(?:/([a-z-]+))?$", path)
    if match:
        return match.group(1), match.group(2) or ""
    match = re.match(r"^/api/ad-material/tasks/([0-9a-f]{32})/assets/([^/]+)/review$", path)
    if match:
        return match.group(1), "asset-review:%s" % match.group(2)
    return "", ""


def list_products(force=False):





























    with PRODUCT_CACHE_LOCK:































        if not force and PRODUCT_CACHE["items"] and now_ts() - PRODUCT_CACHE["updated_at"] < 600:































            return PRODUCT_CACHE["items"]































    items = []































    if os.path.isfile(PRODUCTS_FILE):































        try:































            with open(PRODUCTS_FILE, "r") as fh:































                raw_items = json.load(fh)































            for item in raw_items:































                items.append(































                    {































                        "app_id": str(item.get("app_id", "")).strip(),































                        "app": str(item.get("app", "")).strip(),































                        "country": str(item.get("country", "")).strip(),































                        "language": str(item.get("language", "")).strip(),































                        "label": str(item.get("label", "")).strip()































                        or "%s | %s | %s | %s"































                        % (































                            str(item.get("app_id", "")).strip(),































                            str(item.get("app", "")).strip(),































                            str(item.get("country", "")).strip(),































                            str(item.get("language", "")).strip(),































                        ),































                    }































                )































        except Exception:































            logging.exception("failed to load products file")































    if not items:































        with JOB_DB_LOCK:































            conn = get_job_db_connection()































            try:































                rows = conn.execute(































                    "SELECT DISTINCT app_id, app, country, language FROM drama_material_job ORDER BY updated_at DESC LIMIT 100"































                ).fetchall()































            finally:































                conn.close()































        for row in rows:































            items.append(































                {































                    "app_id": row[0],































                    "app": row[1],































                    "country": row[2],































                    "language": row[3],































                    "label": "%s | %s | %s | %s" % (row[0], row[1], row[2], row[3]),































                }































            )































    with PRODUCT_CACHE_LOCK:































        PRODUCT_CACHE["items"] = items































        PRODUCT_CACHE["updated_at"] = now_ts()































    return items































































































def product_name_for_app_id(app_id, fallback=""):































    app_id = str(app_id or "").strip()































    for item in list_products():































        if str(item.get("app_id", "")).strip() == app_id:































            return str(item.get("app", "")).strip() or fallback































    return fallback































































































def prune_feishu_login_states():































    now_epoch = int(time.time())































    with AUTH_CACHE_LOCK:































        expired = [key for key, value in FEISHU_LOGIN_STATES.items() if value.get("expires_at", 0) <= now_epoch]































        for key in expired:































            FEISHU_LOGIN_STATES.pop(key, None)































































































def create_feishu_login_state(next_path="/"):































    prune_feishu_login_states()































    state = secrets.token_urlsafe(24)































    next_path = str(next_path or "/").strip() or "/"































    if not next_path.startswith("/"):































        next_path = "/"































    with AUTH_CACHE_LOCK:































        FEISHU_LOGIN_STATES[state] = {































            "next_path": next_path,































            "expires_at": int(time.time()) + AUTH_STATE_TTL_SECONDS,































        }































    return state































































































def pop_feishu_login_state(state):































    prune_feishu_login_states()































    with AUTH_CACHE_LOCK:































        item = FEISHU_LOGIN_STATES.pop(state, None)































    return item































































































def build_feishu_login_url(state):































    query = urlencode(































        {































            "app_id": FEISHU_APP_ID,































            "redirect_uri": FEISHU_REDIRECT_URI,































            "response_type": "code",































            "scope": FEISHU_SCOPE,































            "state": state,































        }































    )































    return FEISHU_AUTHORIZE_URL + "?" + query































































































def get_feishu_app_access_token():















    if not feishu_auth_enabled():































        raise ValueError("feishu auth not configured")































    now_epoch = int(time.time())































    with AUTH_CACHE_LOCK:































        cached = dict(FEISHU_APP_ACCESS_TOKEN_CACHE)































    if cached.get("token") and cached.get("expires_at", 0) > now_epoch + 60:































        return cached["token"]































    response = requests.post(































        FEISHU_APP_ACCESS_TOKEN_URL,































        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},































        timeout=30,































    )































    data = response.json()































    if response.status_code >= 400 or data.get("code") not in (0, None):































        raise RuntimeError(data.get("msg") or "failed to fetch feishu app access token")































    token = data.get("app_access_token", "")































    expires_in = int(data.get("expire", 7200) or 7200)































    with AUTH_CACHE_LOCK:































        FEISHU_APP_ACCESS_TOKEN_CACHE["token"] = token































        FEISHU_APP_ACCESS_TOKEN_CACHE["expires_at"] = now_epoch + expires_in































    return token















































def get_feishu_tenant_access_token():















    if not feishu_auth_enabled():















        raise ValueError("feishu auth not configured")















    now_epoch = int(time.time())















    with AUTH_CACHE_LOCK:















        cached = dict(FEISHU_TENANT_ACCESS_TOKEN_CACHE)















    if cached.get("token") and cached.get("expires_at", 0) > now_epoch + 60:















        return cached["token"]















    response = requests.post(















        FEISHU_TENANT_ACCESS_TOKEN_URL,















        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},















        timeout=30,















    )















    data = response.json()















    if response.status_code >= 400 or data.get("code") not in (0, None):















        raise RuntimeError(data.get("msg") or "failed to fetch feishu tenant access token")















    token = data.get("tenant_access_token", "")















    expires_in = int(data.get("expire", 7200) or 7200)















    with AUTH_CACHE_LOCK:















        FEISHU_TENANT_ACCESS_TOKEN_CACHE["token"] = token















        FEISHU_TENANT_ACCESS_TOKEN_CACHE["expires_at"] = now_epoch + expires_in















    return token















































def send_feishu_text(receive_id_type, receive_id, text):















    receive_id = str(receive_id or "").strip()















    if not receive_id:















        raise ValueError("missing feishu receive id")















    tenant_access_token = get_feishu_tenant_access_token()















    response = requests.post(















        FEISHU_MESSAGE_URL + "?" + urlencode({"receive_id_type": receive_id_type}),















        headers={"Authorization": "Bearer " + tenant_access_token},















        json={















            "receive_id": receive_id,















            "msg_type": "text",















            "content": json.dumps({"text": text}, ensure_ascii=False),















        },















        timeout=30,















    )















    data = response.json()















    if response.status_code >= 400 or data.get("code") not in (0, None):















        raise RuntimeError(data.get("msg") or "failed to send feishu message")















    return data















































def build_job_completion_message(job):















    return "\u5267\u96c6\u5408\u6210\u5df2\u5b8c\u6210\uff0c\u8bf7\u53ca\u65f6\u67e5\u770b\n%s\n\u2014\u2014\u2014\u2014\u2014\u2014" % SITE_BASE_URL























def build_job_failure_message(job):















    return "\u5267\u96c6\u5408\u6210\u4efb\u52a1\u5931\u8d25\uff0c\u8bf7\u53ca\u65f6\u67e5\u770b\n%s\n\u2014\u2014\u2014\u2014\u2014\u2014" % SITE_BASE_URL























def build_job_status_message(job):















    if job.get("status") == "failed":















        return build_job_failure_message(job)















    return build_job_completion_message(job)























def mark_job_notification(job, notified_at="", error=""):















    job["completion_notified_at"] = notified_at















    job["completion_notification_error"] = error















    with JOB_DB_LOCK:















        conn = get_job_db_connection()















        try:















            conn.execute(















                """















                UPDATE drama_material_job















                SET completion_notified_at = ?,















                    completion_notification_error = ?,















                    updated_at = CURRENT_TIMESTAMP















                WHERE job_id = ?















                """,















                (notified_at, error, job["job_id"]),















            )















            conn.commit()















        finally:















            conn.close()















































def notify_job_creator_on_completion(job):















    if job.get("status") not in ("done", "failed"):















        return















    if str(job.get("completion_notified_at", "") or "").strip():















        return















    creator_user_id = str(job.get("creator_user_id", "") or "").strip()















    creator_open_id = str(job.get("creator_open_id", "") or "").strip()















    actor = {















        "user_id": creator_user_id,















        "name": str(job.get("creator_name", "") or ""),















    }















    if not creator_user_id and not creator_open_id:















        logging.info("skip completion notification without creator: %s", job.get("job_id"))















        append_audit_log(















            actor,















            "notify_completion_skipped",















            "job",















            job.get("job_id", ""),















            {"reason": "missing_creator"},















        )















        return















    try:















        if creator_user_id:















            send_feishu_text("user_id", creator_user_id, build_job_status_message(job))















            receive_id_type = "user_id"















            receive_id = creator_user_id















        else:















            send_feishu_text("open_id", creator_open_id, build_job_status_message(job))















            receive_id_type = "open_id"















            receive_id = creator_open_id















        mark_job_notification(job, notified_at=now_text(), error="")















        append_audit_log(















            actor,















            "notify_completion_sent",















            "job",















            job.get("job_id", ""),















            {"receive_id_type": receive_id_type, "receive_id": receive_id},















        )















        logging.info("sent completion notification: job=%s %s=%s", job.get("job_id"), receive_id_type, receive_id)















    except Exception as exc:















        logging.exception("failed to send completion notification: job=%s", job.get("job_id"))















        error = str(exc).strip() or exc.__class__.__name__















        mark_job_notification(job, notified_at="", error=error)















        append_audit_log(















            actor,















            "notify_completion_failed",















            "job",















            job.get("job_id", ""),















            {"error": error},















        )















































def exchange_feishu_code_for_user(code):















    app_access_token = get_feishu_app_access_token()































    response = requests.post(































        FEISHU_USER_ACCESS_TOKEN_URL,































        headers={"Authorization": "Bearer " + app_access_token},































        json={"grant_type": "authorization_code", "code": code},































        timeout=30,































    )































    data = response.json()































    if response.status_code >= 400 or data.get("code") not in (0, None):































        raise RuntimeError(data.get("msg") or "failed to exchange feishu code")































    user_token = ((data.get("data") or {}).get("access_token")) or data.get("access_token")































    token_type = ((data.get("data") or {}).get("token_type")) or "Bearer"































    if not user_token:































        raise RuntimeError("missing feishu user access token")































    user_info_resp = requests.get(































        FEISHU_USER_INFO_URL,































        headers={"Authorization": "%s %s" % (token_type, user_token)},































        timeout=30,































    )































    user_info = user_info_resp.json()































    if user_info_resp.status_code >= 400 or user_info.get("code") not in (0, None):































        raise RuntimeError(user_info.get("msg") or "failed to fetch feishu user info")































    info = user_info.get("data") or {}































    if not info.get("user_id") and (data.get("data") or {}).get("open_id"):































        info["open_id"] = (data.get("data") or {}).get("open_id")































    if not info.get("name") and (data.get("data") or {}).get("name"):































        info["name"] = (data.get("data") or {}).get("name")































    return info































































































def create_user_session(user_info, source):































    assert_feishu_user_allowed(user_info)































    cleanup_expired_sessions()































    session_token = secrets.token_urlsafe(32)































    now_epoch = int(time.time())































    expires_at = now_epoch + SESSION_TTL_SECONDS































    role = default_role_for_user(user_info)































    record = {































        "session_token": session_token,































        "user_id": str(user_info.get("user_id", "") or ""),































        "union_id": str(user_info.get("union_id", "") or ""),































        "open_id": str(user_info.get("open_id", "") or ""),































        "name": str(user_info.get("name", "") or ""),































        "en_name": str(user_info.get("en_name", "") or ""),

        "email": str(user_info.get("email", "") or user_info.get("enterprise_email", "") or ""),































        "avatar_url": str(user_info.get("avatar_url", "") or ""),































        "tenant_key": str(user_info.get("tenant_key", "") or ""),































        "source": str(source or ""),































        "role": role,































        "permissions": normalize_user_permissions({}, role),































        "expires_at": expires_at,































    }































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            conn.execute(































                """































                INSERT INTO drama_admin_user (































                  user_id, union_id, open_id, name, en_name, email, avatar_url, tenant_key,































                  role, permissions_json, status, last_source, login_count, first_login_at, last_login_at, created_at, updated_at































                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)































                ON CONFLICT(user_id) DO UPDATE SET































                  union_id=excluded.union_id,































                  open_id=excluded.open_id,































                  name=excluded.name,































                  en_name=excluded.en_name,

                  email=excluded.email,































                  avatar_url=excluded.avatar_url,































                  tenant_key=excluded.tenant_key,































                  role=CASE































                    WHEN drama_admin_user.user_id IN ({admin_ids}) OR drama_admin_user.name IN ({admin_names})































                      THEN 'admin'































                    ELSE drama_admin_user.role































                  END,































                  permissions_json=CASE































                    WHEN drama_admin_user.user_id IN ({admin_ids}) OR drama_admin_user.name IN ({admin_names})































                      THEN ?































                    WHEN TRIM(drama_admin_user.permissions_json) = '' OR drama_admin_user.permissions_json = '{{}}'





























                      THEN excluded.permissions_json































                    ELSE drama_admin_user.permissions_json































                  END,































                  status='active',































                  last_source=excluded.last_source,































                  login_count=drama_admin_user.login_count + 1,































                  last_login_at=CURRENT_TIMESTAMP,































                  updated_at=CURRENT_TIMESTAMP































                """.format(































                    admin_ids=", ".join(["'%s'" % item.replace("'", "''") for item in ADMIN_USER_IDS]) or "''",































                    admin_names=", ".join(["'%s'" % item.replace("'", "''") for item in ADMIN_NAMES]) or "''",































                ),































                (































                    record["user_id"],































                    record["union_id"],































                    record["open_id"],































                    record["name"],































                    record["en_name"],

                    record["email"],































                    record["avatar_url"],































                    record["tenant_key"],































                    role,































                    json.dumps(normalize_user_permissions({}, role), ensure_ascii=False),































                    record["source"],































                    json.dumps(ADMIN_PERMISSIONS, ensure_ascii=False),































                ),































            )































            conn.execute(































                """































                INSERT INTO drama_admin_session (































                  session_token, user_id, union_id, open_id, name, en_name,































                  avatar_url, tenant_key, source, expires_at, created_at, updated_at































                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)































                """,































                (































                    record["session_token"],































                    record["user_id"],































                    record["union_id"],































                    record["open_id"],































                    record["name"],































                    record["en_name"],































                    record["avatar_url"],































                    record["tenant_key"],































                    record["source"],































                    record["expires_at"],































                ),































            )































            conn.execute(































                """































                INSERT INTO drama_admin_audit_log (































                  actor_user_id, actor_name, action, target_type, target_id, detail_json, created_at































                ) VALUES (?, ?, 'login', 'session', ?, ?, CURRENT_TIMESTAMP)































                """,































                (































                    record["user_id"],































                    record["name"],































                    record["session_token"],































                    json.dumps(































                        {































                            "source": record["source"],































                            "tenant_key": record["tenant_key"],































                            "role": role,































                        },































                        ensure_ascii=False,































                    ),































                ),































            )































            conn.commit()































        finally:































            conn.close()































    return record































































































def load_session(session_token):































    if not session_token:































        return None































    cleanup_expired_sessions()































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            row = conn.execute(































                """































                SELECT s.session_token, s.user_id, s.union_id, s.open_id, s.name, s.en_name,































                       s.avatar_url, s.tenant_key, s.source, s.expires_at,































                       COALESCE(u.role, ?) AS role,































                       COALESCE(u.permissions_json, '{}') AS permissions_json,

                       COALESCE(u.email, '') AS email































                FROM drama_admin_session s































                LEFT JOIN drama_admin_user u ON u.user_id = s.user_id































                WHERE s.session_token = ?































                """,































                (default_role_for_user({"user_id": "", "name": ""}), session_token),































            ).fetchone()































        finally:































            conn.close()































    if not row:































        return None































    return {































        "session_token": row[0],































        "user_id": row[1],































        "union_id": row[2],































        "open_id": row[3],































        "name": row[4],































        "en_name": row[5],































        "avatar_url": row[6],































        "tenant_key": row[7],































        "source": row[8],































        "expires_at": int(row[9]),































        "role": row[10] or "user",































        "permissions": normalize_user_permissions(row[11] if len(row) > 11 else {}, row[10] or "user"),

        "email": row[12] if len(row) > 12 else "",































    }































































































def delete_session(session_token):































    if not session_token:































        return































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            row = conn.execute(































                "SELECT user_id, name FROM drama_admin_session WHERE session_token = ?",































                (session_token,),































            ).fetchone()































            conn.execute("DELETE FROM drama_admin_session WHERE session_token = ?", (session_token,))































            if row:































                conn.execute(































                    """































                    INSERT INTO drama_admin_audit_log (































                      actor_user_id, actor_name, action, target_type, target_id, detail_json, created_at































                    ) VALUES (?, ?, 'logout', 'session', ?, '{}', CURRENT_TIMESTAMP)































                    """,































                    (row[0] or "", row[1] or "", session_token),































                )































            conn.commit()































        finally:































            conn.close()































































































def load_navigation_config():
    with open(NAVIGATION_CONFIG_PATH, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def validate_navigation_config(config):
    if not isinstance(config, list):
        raise ValueError("导航配置必须是数组")
    for group in config:
        if not isinstance(group, dict):
            raise ValueError("导航分组必须是对象")
        if not str(group.get("key", "")).strip():
            raise ValueError("导航分组缺少 key")
        if not str(group.get("label", "")).strip():
            raise ValueError("导航分组缺少 label")
        items = group.get("items", [])
        if not isinstance(items, list):
            raise ValueError("导航分组 items 必须是数组")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("导航项必须是对象")
            if not str(item.get("key", "")).strip():
                raise ValueError("导航项缺少 key")
            if not str(item.get("label", "")).strip():
                raise ValueError("导航项缺少 label")
            if not str(item.get("href", "")).strip():
                raise ValueError("导航项缺少 href")
    return config


def save_navigation_config(config):
    config = validate_navigation_config(config)
    directory = os.path.dirname(NAVIGATION_CONFIG_PATH)
    os.makedirs(directory, exist_ok=True)
    temp_path = NAVIGATION_CONFIG_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, NAVIGATION_CONFIG_PATH)
    return config


def list_admin_users():































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            rows = conn.execute(































                """































                SELECT user_id, union_id, open_id, name, en_name, avatar_url, tenant_key,































                       role, permissions_json, status, last_source, login_count, first_login_at, last_login_at,































                       created_at, updated_at































                FROM drama_admin_user































                WHERE last_source LIKE 'feishu%'































                ORDER BY































                  CASE role WHEN 'admin' THEN 1 ELSE 0 END DESC,































                  last_login_at DESC,































                  created_at DESC































                """































            ).fetchall()































        finally:































            conn.close()































    items = []































    for row in rows:































        items.append(































            {































                "user_id": row[0],































                "union_id": row[1],































                "open_id": row[2],































                "name": row[3],































                "en_name": row[4],































                "avatar_url": row[5],































                "tenant_key": row[6],































                "role": row[7],































                "permissions": normalize_user_permissions(row[8], row[7]),































                "status": row[9],































                "last_source": row[10],































                "login_count": int(row[11] or 0),































                "first_login_at": row[12],































                "last_login_at": row[13],































                "created_at": row[14],































                "updated_at": row[15],































            }































        )































    return items































































































def update_admin_user_role(target_user_id, role, actor_session):































    role = "admin" if str(role or "").strip() == "admin" else "user"































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            row = conn.execute(































                "SELECT user_id, name, role FROM drama_admin_user WHERE user_id = ?",































                (target_user_id,),































            ).fetchone()































            if not row:































                raise ValueError("用户不存在")































            conn.execute(































                "UPDATE drama_admin_user SET role = ?, permissions_json = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",































                (role, json.dumps(normalize_user_permissions({}, role), ensure_ascii=False), target_user_id),































            )































            conn.execute(































                """































                INSERT INTO drama_admin_audit_log (































                  actor_user_id, actor_name, action, target_type, target_id, detail_json, created_at































                ) VALUES (?, ?, 'update_role', 'user', ?, ?, CURRENT_TIMESTAMP)































                """,































                (































                    actor_session.get("user_id", ""),































                    actor_session.get("name", ""),































                    target_user_id,































                    json.dumps({"role": role}, ensure_ascii=False),































                ),































            )































            conn.commit()































        finally:































            conn.close()































    return {"user_id": target_user_id, "role": role}































































































def update_admin_user_permissions(target_user_id, permissions, actor_session):































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            row = conn.execute(































                "SELECT user_id, name, role, permissions_json FROM drama_admin_user WHERE user_id = ?",































                (target_user_id,),































            ).fetchone()































            if not row:































                raise ValueError("用户不存在")































            if row[2] == "admin":































                normalized = dict(ADMIN_PERMISSIONS)































            else:































                normalized = normalize_user_permissions(permissions, row[2])































            conn.execute(































                "UPDATE drama_admin_user SET permissions_json = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",































                (json.dumps(normalized, ensure_ascii=False), target_user_id),































            )































            conn.execute(































                """































                INSERT INTO drama_admin_audit_log (































                  actor_user_id, actor_name, action, target_type, target_id, detail_json, created_at































                ) VALUES (?, ?, 'update_permissions', 'user', ?, ?, CURRENT_TIMESTAMP)































                """,































                (































                    actor_session.get("user_id", ""),































                    actor_session.get("name", ""),































                    target_user_id,































                    json.dumps({"permissions": normalized}, ensure_ascii=False),































                ),































            )































            conn.commit()































        finally:































            conn.close()































    return {"user_id": target_user_id, "permissions": normalized}































































def append_audit_log(actor_session, action, target_type="", target_id="", detail=None):































    detail = detail or {}































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            conn.execute(































                """































                INSERT INTO drama_admin_audit_log (































                  actor_user_id, actor_name, action, target_type, target_id, detail_json, created_at































                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)































                """,































                (































                    (actor_session or {}).get("user_id", ""),































                    (actor_session or {}).get("name", ""),































                    str(action or ""),































                    str(target_type or ""),































                    str(target_id or ""),































                    json.dumps(detail, ensure_ascii=False),































                ),































            )































            conn.commit()































        finally:































            conn.close()































































































def list_audit_logs(limit=200):































    limit = max(1, min(500, int(limit)))































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            rows = conn.execute(































                """































                SELECT id, actor_user_id, actor_name, action, target_type, target_id, detail_json, created_at































                FROM drama_admin_audit_log































                ORDER BY id DESC































                LIMIT ?































                """,































                (limit,),































            ).fetchall()































        finally:































            conn.close()































    items = []































    for row in rows:































        items.append(































            {































                "id": int(row[0]),































                "actor_user_id": row[1],































                "actor_name": row[2],































                "action": row[3],































                "target_type": row[4],































                "target_id": row[5],































                "detail": parse_json_text(row[6], {}),































                "created_at": row[7],































            }































        )































    return {"items": items}















































def backfill_job_creators_from_audit_logs():















    with JOB_DB_LOCK:















        conn = get_job_db_connection()















        try:















            conn.execute(















                """















                UPDATE drama_material_job















                SET creator_user_id = COALESCE((















                      SELECT l.actor_user_id















                      FROM drama_admin_audit_log l















                      WHERE l.action = 'create_job'















                        AND l.target_type = 'job'















                        AND l.target_id = drama_material_job.job_id















                        AND TRIM(l.actor_user_id) != ''















                      ORDER BY l.id ASC















                      LIMIT 1















                    ), ''),















                    creator_name = COALESCE((















                      SELECT l.actor_name















                      FROM drama_admin_audit_log l















                      WHERE l.action = 'create_job'















                        AND l.target_type = 'job'















                        AND l.target_id = drama_material_job.job_id















                        AND TRIM(l.actor_name) != ''















                      ORDER BY l.id ASC















                      LIMIT 1















                    ), ''),















                    creator_open_id = COALESCE((















                      SELECT u.open_id















                      FROM drama_admin_user u















                      WHERE u.user_id = (















                        SELECT l.actor_user_id















                        FROM drama_admin_audit_log l















                        WHERE l.action = 'create_job'















                          AND l.target_type = 'job'















                          AND l.target_id = drama_material_job.job_id















                          AND TRIM(l.actor_user_id) != ''















                        ORDER BY l.id ASC















                        LIMIT 1















                      )















                      LIMIT 1















                    ), ''),















                    updated_at = CURRENT_TIMESTAMP















                WHERE TRIM(creator_user_id) = ''















                  AND EXISTS (















                    SELECT 1















                    FROM drama_admin_audit_log l















                    WHERE l.action = 'create_job'















                      AND l.target_type = 'job'















                      AND l.target_id = drama_material_job.job_id















                      AND TRIM(l.actor_user_id) != ''















                  )















                """















            )















            conn.commit()















        finally:















            conn.close()















































def pick_drama_variant(content_id, app_id=None):















    app_id_filter = ""































    if app_id:































        app_id_filter = " AND app_id='%s'" % shell_quote(str(app_id))































    query = """































    SELECT app, country, language, COUNT(*) AS cnt, MAX(updated_at) AS latest_update































    FROM {db}.{table}































    WHERE content_id='{content_id}'































      {app_id_filter}































      AND type=2































      AND sub_number > 0































      AND sub_url <> ''































    GROUP BY app, country, language































    ORDER BY cnt DESC, latest_update DESC































    LIMIT 1































    """.format(































        db=DB_NAME,































        table=SOURCE_TABLE,































        content_id=shell_quote(content_id),































        app_id_filter=app_id_filter,































    )































    rows = run_mysql(" ".join(query.split()))































    if not rows:































        return None































    return {"app": rows[0][0], "country": rows[0][1], "language": rows[0][2]}































































































def fetch_drama_episodes(content_id, app_id=None):































    variant = pick_drama_variant(content_id, app_id=app_id)































    if not variant:































        raise ValueError("app_id=%s content_id=%s 未找到可用剧集资源" % (app_id, content_id))































    query = """































    SELECT































      CAST(app_id AS CHAR), name, cover, sub_number, sub_url, all_episodes_count, app, country, language































    FROM {db}.{table}































    WHERE content_id='{content_id}'































      AND app_id='{app_id}'































      AND app='{app}'































      AND country='{country}'































      AND language='{language}'































      AND type=2































      AND sub_number > 0































      AND sub_url <> ''































    ORDER BY sub_number ASC































    """.format(































        db=DB_NAME,































        table=SOURCE_TABLE,































        content_id=shell_quote(content_id),































        app_id=shell_quote(str(app_id)),































        app=shell_quote(variant["app"]),































        country=shell_quote(variant["country"]),































        language=shell_quote(variant["language"]),































    )































    rows = run_mysql(" ".join(query.split()))































    if not rows:































        raise ValueError("app_id=%s content_id=%s 查询结果为空" % (app_id, content_id))































    items = []































    for row in rows:































        items.append(































            {































                "app_id": row[0],































                "drama_name": row[1],































                "cover_url": row[2],































                "episode_number": int(row[3]),































                "episode_url": row[4],































                "all_episodes_count": int(row[5]),































                "app": row[6],































                "country": row[7],































                "language": row[8],































            }































        )































    return items































































































def validate_content_request(app_id, content_id, episode_start, episode_end):































    episodes = fetch_drama_episodes(content_id, app_id=app_id)































    available_numbers = [episode["episode_number"] for episode in episodes]































    available_start = min(available_numbers)































    available_end = max(available_numbers)































    if episode_start < available_start or episode_end > available_end:































        raise ValueError(































            "集数范围无效，可用范围为 %d-%d，请求范围为 %d-%d"































            % (available_start, available_end, episode_start, episode_end)































        )































    requested = [































        episode for episode in episodes if episode_start <= episode["episode_number"] <= episode_end































    ]































    if not requested:































        raise ValueError("请求范围内没有可用剧集资源")































    sample = requested[0]































    return {































        "requested": requested,































        "app_id": sample["app_id"],































        "total_episodes": len(episodes),































        "app": product_name_for_app_id(sample["app_id"], sample["app"]),































        "country": sample["country"],































        "language": sample["language"],































        "drama_name": sample["drama_name"],































        "cover_source_url": normalize_cover_source_url(sample["cover_url"] or episodes[0]["cover_url"]),































        "available_episode_start": available_start,































        "available_episode_end": available_end,































    }































































































def validate_screenshot_request(app_id, content_id):

    episodes = fetch_drama_episodes(content_id, app_id=app_id)

    sample = episodes[0]

    cover_source_url = ""

    for episode in episodes:

        cover_source_url = normalize_cover_source_url(episode.get("cover_url", ""))

        if cover_source_url:

            break

    if not cover_source_url:

        raise ValueError("未找到可用封面素材")

    return {

        "app_id": sample["app_id"],

        "total_episodes": len(episodes),

        "app": product_name_for_app_id(sample["app_id"], sample["app"]),

        "country": sample["country"],

        "language": sample["language"],

        "drama_name": sample["drama_name"],

        "cover_source_url": cover_source_url,

    }





def default_screenshot_service_url_for_item(item):
    key = str(item.get("key", "") or "").strip()
    return CODEX_SCREENSHOT_SERVICE_URLS.get(key) or CODEX_SCREENSHOT_SERVICE_URLS["square_1x1"]


def screenshot_service_pool_enabled():
    if not CODEX_SCREENSHOT_SERVICE_POOL:
        return False
    if not CODEX_SCREENSHOT_SERVICE_POOL_BURST_ONLY:
        return True
    if SCREENSHOT_JOB_ACTIVE_COUNT > SCREENSHOT_JOB_BASE_CONCURRENCY:
        return True
    return screenshot_job_backlog_count() > SCREENSHOT_JOB_BURST_QUEUE_THRESHOLD


def choose_screenshot_service_url(default_url):
    global SCREENSHOT_SERVICE_POOL_INDEX
    if not screenshot_service_pool_enabled():
        return default_url
    with SCREENSHOT_SERVICE_POOL_LOCK:
        pool = list(CODEX_SCREENSHOT_SERVICE_POOL)
        if not pool:
            return default_url
        min_load = min(SCREENSHOT_SERVICE_POOL_INFLIGHT.get(item, 0) for item in pool)
        start = SCREENSHOT_SERVICE_POOL_INDEX % len(pool)
        selected_index = start
        for offset in range(len(pool)):
            index = (start + offset) % len(pool)
            candidate = pool[index]
            if SCREENSHOT_SERVICE_POOL_INFLIGHT.get(candidate, 0) == min_load:
                selected_index = index
                break
        url = pool[selected_index]
        SCREENSHOT_SERVICE_POOL_INDEX = selected_index + 1
    return url


def acquire_screenshot_service_url(default_url):
    url = choose_screenshot_service_url(default_url)
    with SCREENSHOT_SERVICE_POOL_LOCK:
        SCREENSHOT_SERVICE_POOL_INFLIGHT[url] = SCREENSHOT_SERVICE_POOL_INFLIGHT.get(url, 0) + 1
    return url


def release_screenshot_service_url(url):
    if not url:
        return
    with SCREENSHOT_SERVICE_POOL_LOCK:
        count = SCREENSHOT_SERVICE_POOL_INFLIGHT.get(url, 0)
        if count <= 1:
            SCREENSHOT_SERVICE_POOL_INFLIGHT.pop(url, None)
        else:
            SCREENSHOT_SERVICE_POOL_INFLIGHT[url] = count - 1


def generate_screenshot_via_codex_service(job, source_path, items):
    payload = {
        "job_id": job["job_id"],
        "app_id": job.get("app_id", ""),
        "content_id": job["content_id"],
        "drama_name": job.get("drama_name", ""),
        "source_path": source_path,
        "source_url": job.get("cover_source_url", ""),
        "items": items,
    }
    default_service_url = default_screenshot_service_url_for_item(items[0]) if items else CODEX_SCREENSHOT_SERVICE_URLS["square_1x1"]
    service_url = acquire_screenshot_service_url(default_service_url)
    try:
        response = requests.post(
            service_url,
            json=payload,
            timeout=CODEX_SCREENSHOT_SERVICE_TIMEOUT,
        )
    finally:
        release_screenshot_service_url(service_url)
    try:
        data = response.json()
    except Exception:
        data = {"error": response.text.strip()}
    if response.status_code >= 400:
        raise RuntimeError(
            "codex screenshot service error (%s): %s"
            % (response.status_code, data.get("error") or response.text.strip())
        )
    if data.get("status") != "done":
        raise RuntimeError("codex screenshot service returned unexpected payload")
    record_screenshot_token_usage(job, data.get("token_usage") or data.get("usage"), "codex_screenshot_single")
    requested_by_key = {str(item.get("key", "")): item for item in items}
    for result_item in data.get("items", []) or []:
        key = str(result_item.get("key", ""))
        requested_item = requested_by_key.get(key)
        remote_url = str(result_item.get("public_url") or "").strip()
        if not requested_item or not remote_url:
            continue
        workspace_output_path = requested_item.get("workspace_output_path")
        public_output_path = requested_item.get("public_output_path")
        if workspace_output_path and public_output_path and not file_ready(public_output_path):
            download_file(remote_url, workspace_output_path)
            ensure_dir(os.path.dirname(public_output_path))
            shutil.copy2(workspace_output_path, public_output_path)
            result_item["workspace_output_path"] = workspace_output_path
            result_item["public_output_path"] = public_output_path
    return data


def generate_screenshot_via_codex_service_batch(job, source_path, items):
    payload = {
        "job_id": job["job_id"],
        "app_id": job.get("app_id", ""),
        "content_id": job["content_id"],
        "drama_name": job.get("drama_name", ""),
        "source_path": source_path,
        "source_url": job.get("cover_source_url", ""),
        "items": items,
    }
    service_url = acquire_screenshot_service_url(CODEX_SCREENSHOT_SERVICE_URLS.get("square_1x1") or CODEX_SCREENSHOT_SERVICE_URL)
    try:
        response = requests.post(
            service_url,
            json=payload,
            timeout=CODEX_SCREENSHOT_SERVICE_TIMEOUT,
        )
    finally:
        release_screenshot_service_url(service_url)
    try:
        data = response.json()
    except Exception:
        data = {"error": response.text.strip()}
    if response.status_code >= 400:
        raise RuntimeError(
            "codex screenshot service error (%s): %s"
            % (response.status_code, data.get("error") or response.text.strip())
        )
    if data.get("status") != "done":
        raise RuntimeError("codex screenshot service returned unexpected payload")
    record_screenshot_token_usage(job, data.get("token_usage") or data.get("usage"), "codex_screenshot_batch")
    requested_by_key = {str(item.get("key", "")): item for item in items}
    for result_item in data.get("items", []) or []:
        key = str(result_item.get("key", ""))
        requested_item = requested_by_key.get(key)
        remote_url = str(result_item.get("public_url") or "").strip()
        if not requested_item or not remote_url:
            continue
        workspace_output_path = requested_item.get("workspace_output_path")
        public_output_path = requested_item.get("public_output_path")
        if workspace_output_path and public_output_path and not file_ready(public_output_path):
            download_file(remote_url, workspace_output_path)
            ensure_dir(os.path.dirname(public_output_path))
            shutil.copy2(workspace_output_path, public_output_path)
            result_item["workspace_output_path"] = workspace_output_path
            result_item["public_output_path"] = public_output_path
    return data


def run_cmd(cmd, timeout=None):





























    logging.info("running: %s", " ".join(cmd))































    proc = subprocess.run(































        cmd,































        stdout=subprocess.PIPE,































        stderr=subprocess.PIPE,































        universal_newlines=True,































        timeout=timeout,































    )































    if proc.returncode != 0:































        raise RuntimeError(































            "command failed (%s): %s" % (proc.returncode, proc.stderr.strip() or proc.stdout.strip())































        )































    return proc































































































def download_file(url, output_path, progress_callback=None):































    logging.info("downloading %s -> %s", url, output_path)































    response = requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT)































    response.raise_for_status()































    total = int(response.headers.get("Content-Length", "0") or 0)































    downloaded = 0































    last_reported = -1































    if progress_callback:































        progress_callback(downloaded, total)































    with open(output_path, "wb") as fh:































        for chunk in response.iter_content(chunk_size=1024 * 1024):































            if chunk:































                fh.write(chunk)































                downloaded += len(chunk)































                if progress_callback:































                    percent = int(downloaded * 100 / total) if total > 0 else -1































                    if total <= 0 or percent >= last_reported + 5 or downloaded == total:































                        last_reported = percent































                        progress_callback(downloaded, total)































    if progress_callback:































        progress_callback(downloaded or 1, total or downloaded or 1)































































































def generate_cover_via_codex_service(job, source_path, workspace_output_path, public_output_path):































    payload = {































        "job_id": job["job_id"],































        "app_id": job.get("app_id", ""),































        "content_id": job["content_id"],































        "drama_name": job.get("drama_name", ""),































        "source_path": source_path,































        "source_url": job.get("cover_source_url", ""),
        "workspace_output_path": workspace_output_path,































        "public_output_path": public_output_path,































    }































    response = requests.post(CODEX_COVER_SERVICE_URL, json=payload, timeout=CODEX_COVER_SERVICE_TIMEOUT)































    try:































        data = response.json()































    except Exception:































        data = {"error": response.text.strip()}































    if response.status_code >= 400:































        raise RuntimeError("codex cover service error (%s): %s" % (response.status_code, data.get("error") or response.text.strip()))































    if data.get("status") != "done":































        raise RuntimeError("codex cover service returned unexpected payload")
    remote_cover_url = str(data.get("public_url") or "").strip()
    if remote_cover_url and not file_ready(public_output_path):
        download_file(remote_cover_url, workspace_output_path)
        ensure_dir(os.path.dirname(public_output_path))
        shutil.copy2(workspace_output_path, public_output_path)
        data["workspace_output_path"] = workspace_output_path
        data["public_output_path"] = public_output_path
    if not file_ready(public_output_path):
        raise RuntimeError("cover service did not create output: %s" % public_output_path)































    return data

































































































def ffmpeg_has_encoder(encoder):
    encoder = str(encoder or "").strip()
    if not encoder:
        return False
    if encoder not in _FFMPEG_ENCODER_CACHE:
        try:
            output = subprocess.check_output([FFMPEG, "-hide_banner", "-encoders"], stderr=subprocess.STDOUT, universal_newlines=True, timeout=20)
            _FFMPEG_ENCODER_CACHE[encoder] = encoder in output
        except Exception as exc:
            logging.warning("failed to inspect ffmpeg encoders: %s", exc)
            _FFMPEG_ENCODER_CACHE[encoder] = False
    return _FFMPEG_ENCODER_CACHE[encoder]


def video_encode_args():
    mode = FFMPEG_VIDEO_ENCODER or "auto"
    wants_nvenc = mode in ("auto", "nvenc", "h264_nvenc")
    if wants_nvenc and ffmpeg_has_encoder("h264_nvenc"):
        return ["-c:v", "h264_nvenc", "-preset", FFMPEG_NVENC_PRESET or "fast", "-cq", FFMPEG_NVENC_CQ or "23", "-b:v", "0"]
    if mode in ("nvenc", "h264_nvenc"):
        logging.warning("requested h264_nvenc but current ffmpeg has no h264_nvenc encoder; falling back to libx264")
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]


def ffmpeg_has_filter(filter_name):
    filter_name = str(filter_name or "").strip()
    if not filter_name:
        return False
    if filter_name not in _FFMPEG_FILTER_CACHE:
        try:
            output = subprocess.check_output([FFMPEG, "-hide_banner", "-filters"], stderr=subprocess.STDOUT, universal_newlines=True, timeout=20)
            _FFMPEG_FILTER_CACHE[filter_name] = filter_name in output
        except Exception as exc:
            logging.warning("failed to inspect ffmpeg filters: %s", exc)
            _FFMPEG_FILTER_CACHE[filter_name] = False
    return _FFMPEG_FILTER_CACHE[filter_name]


def episode_filter_args():
    cpu_filter = "[0:v]split=2[bg][fg];[bg]scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,boxblur=30:12[bg2];[fg]scale=405:720:force_original_aspect_ratio=decrease[fg2];[bg2][fg2]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
    cuda_filter = "[0:v]hwupload_cuda,split=2[bg][fg];[bg]scale_cuda=1280:720:reset_sar=1,bilateral_cuda=sigmaS=16:sigmaR=0.2:window_size=9[bg2];[fg]scale_cuda=405:720:force_original_aspect_ratio=decrease:reset_sar=1[fg2];[bg2][fg2]overlay_cuda=x=(W-w)/2:y=(H-h)/2,hwdownload,format=yuv420p,setsar=1[v]"
    backend = FFMPEG_FILTER_BACKEND or "auto"
    wants_cuda = backend in ("auto", "cuda")
    has_cuda_filters = all(ffmpeg_has_filter(name) for name in ("hwupload_cuda", "scale_cuda", "bilateral_cuda", "overlay_cuda"))
    if wants_cuda and has_cuda_filters:
        return ["-filter_complex", cuda_filter]
    if backend == "cuda":
        logging.warning("requested cuda filter backend but current ffmpeg lacks required filters; falling back to cpu filters")
    return ["-filter_complex", cpu_filter]


def normalize_episode(source_path, output_path):
    run_cmd([
        FFMPEG, "-y", "-i", source_path,
        *episode_filter_args(),
        "-map", "[v]", "-map", "0:a?", "-r", "25", *video_encode_args(),
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", output_path,
    ])


def normalize_concat_segment(source_path, output_path, fps="25", audio_rate="48000"):
    ensure_dir(os.path.dirname(output_path))
    tmp_output_path = output_path + ".tmp.%s.mp4" % os.getpid()
    if os.path.exists(tmp_output_path):
        os.remove(tmp_output_path)
    try:
        run_cmd([
            FFMPEG, "-y", "-i", source_path,
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", "fps=%s,format=yuv420p,setsar=1" % fps,
            "-r", str(fps),
            *video_encode_args(),
            "-c:a", "aac", "-b:a", "128k", "-ar", str(audio_rate), "-ac", "2",
            "-af", "aresample=async=1:first_pts=0",
            "-movflags", "+faststart",
            "-shortest",
            tmp_output_path,
        ])
        if not valid_video_file(tmp_output_path):
            raise RuntimeError("normalized concat segment is not a valid video: %s" % tmp_output_path)
        if not valid_av_duration_alignment(tmp_output_path):
            raise RuntimeError("normalized concat segment has audio/video duration mismatch: %s" % tmp_output_path)
        os.replace(tmp_output_path, output_path)
    finally:
        if os.path.exists(tmp_output_path):
            os.remove(tmp_output_path)


def concat_segments_need_normalization(segment_paths):
    signatures = []
    for path in segment_paths:
        data = probe_media_stream_info(path)
        streams = data.get("streams") or []
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        if not video or not audio:
            return True
        signatures.append((
            video.get("codec_name") or "",
            int(video.get("width") or 0),
            int(video.get("height") or 0),
            video.get("avg_frame_rate") or video.get("r_frame_rate") or "",
            video.get("time_base") or "",
            audio.get("codec_name") or "",
            audio.get("sample_rate") or "",
            int(audio.get("channels") or 0),
            audio.get("time_base") or "",
        ))
    return len(set(signatures)) > 1


def prepare_concat_segments(segment_paths, output_dir):
    if len(segment_paths) <= 1 or not concat_segments_need_normalization(segment_paths):
        return segment_paths
    ensure_dir(output_dir)
    normalized_paths = []
    for index, source_path in enumerate(segment_paths):
        normalized_path = os.path.join(output_dir, "%03d.mp4" % index)
        if (
            not file_ready(normalized_path)
            or not valid_video_file(normalized_path)
            or not valid_av_duration_alignment(normalized_path)
        ):
            normalize_concat_segment(source_path, normalized_path)
        normalized_paths.append(normalized_path)
    return normalized_paths


def ffprobe_path():
    configured = os.environ.get("DRAMA_FFPROBE", "").strip()
    candidates = []
    if configured:
        candidates.append(configured)
    if FFMPEG:
        candidates.append(os.path.join(os.path.dirname(FFMPEG), "ffprobe"))
    candidates.append("ffprobe")
    for candidate in candidates:
        if os.path.isabs(candidate):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        elif shutil.which(candidate):
            return candidate
    return candidates[-1]


def valid_media_rate(value, default_value):
    raw_value = str(value or "").strip()
    if not raw_value:
        return default_value
    if "/" in raw_value:
        left, right = raw_value.split("/", 1)
        try:
            numerator = int(left)
            denominator = int(right)
        except ValueError:
            return default_value
        if numerator > 0 and denominator > 0:
            return raw_value
        return default_value
    try:
        if float(raw_value) > 0:
            return raw_value
    except ValueError:
        return default_value
    return default_value


def probe_intro_reference_timing(reference_path):
    timing = {"fps": "25", "audio_rate": "48000"}
    if not reference_path or not file_ready(reference_path):
        return timing
    probe = ffprobe_path()
    try:
        proc = subprocess.run(
            [
                probe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                "-of", "json", reference_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=30,
        )
        if proc.returncode == 0:
            stream = (json.loads(proc.stdout or "{}").get("streams") or [{}])[0]
            timing["fps"] = valid_media_rate(stream.get("avg_frame_rate") or stream.get("r_frame_rate"), timing["fps"])
    except Exception as exc:
        logging.warning("failed to probe intro reference video timing: %s", exc)
    try:
        proc = subprocess.run(
            [
                probe, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=sample_rate",
                "-of", "json", reference_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=30,
        )
        if proc.returncode == 0:
            stream = (json.loads(proc.stdout or "{}").get("streams") or [{}])[0]
            sample_rate = str(stream.get("sample_rate") or "").strip()
            if sample_rate.isdigit() and int(sample_rate) > 0:
                timing["audio_rate"] = sample_rate
    except Exception as exc:
        logging.warning("failed to probe intro reference audio timing: %s", exc)
    return timing


def render_intro(cover_path, output_path, reference_path=None):
    timing = probe_intro_reference_timing(reference_path)
    intro_fps = timing["fps"]
    intro_audio_rate = timing["audio_rate"]
    if reference_path:
        logging.info("rendering intro with reference timing: fps=%s audio_rate=%s source=%s", intro_fps, intro_audio_rate, reference_path)
    ensure_dir(os.path.dirname(output_path))
    tmp_output_path = output_path + ".tmp.%s.mp4" % os.getpid()
    if os.path.exists(tmp_output_path):
        os.remove(tmp_output_path)































    run_cmd([































        FFMPEG, "-y", "-loop", "1", "-i", cover_path,































        "-f", "lavfi", "-i", "anullsrc=r=%s:cl=stereo" % intro_audio_rate,































        "-t", str(INTRO_SECONDS),































        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=yuv420p",































        "-map", "0:v", "-map", "1:a", "-r", intro_fps, *video_encode_args(),































        "-movflags", "+faststart", "-c:a", "aac", "-b:a", "128k", "-ar", intro_audio_rate, "-ac", "2", "-shortest", tmp_output_path,































    ])
    try:
        if not valid_video_file(tmp_output_path):
            raise RuntimeError("intro output is not a valid video: %s" % tmp_output_path)
        if not valid_av_duration_alignment(tmp_output_path):
            raise RuntimeError("intro output has audio/video duration mismatch: %s" % tmp_output_path)
        os.replace(tmp_output_path, output_path)
    finally:
        if os.path.exists(tmp_output_path):
            os.remove(tmp_output_path)































































































def concat_segments(segment_paths, output_path):































    fd, concat_path = tempfile.mkstemp(prefix="drama_concat_", suffix=".txt")































    os.close(fd)
    ensure_dir(os.path.dirname(output_path))
    tmp_output_path = output_path + ".tmp.%s.mp4" % os.getpid()































    try:































        with open(concat_path, "w") as fh:































            for path in segment_paths:































                fh.write("file '%s'\n" % path.replace("'", "'\\''"))































        if os.path.exists(tmp_output_path):
            os.remove(tmp_output_path)
        run_cmd([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", concat_path, "-c", "copy", "-movflags", "+faststart", tmp_output_path])
        if not valid_video_file(tmp_output_path):
            raise RuntimeError("concat output is not a valid video: %s" % tmp_output_path)
        if not valid_av_duration_alignment(tmp_output_path):
            raise RuntimeError("concat output has audio/video duration mismatch: %s" % tmp_output_path)
        os.replace(tmp_output_path, output_path)































    finally:































        if os.path.exists(concat_path):































            os.remove(concat_path)
        if os.path.exists(tmp_output_path):
            os.remove(tmp_output_path)































































































def extract_audio_for_demucs(input_video_path, output_audio_path):































    run_cmd([































        FFMPEG, "-y", "-i", input_video_path, "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", output_audio_path,































    ], timeout=DEMUCS_TIMEOUT)































































































def extract_vocals_with_demucs(input_audio_path, output_vocals_path, model=None, segment=None, shifts=None, jobs=None):































    ensure_dir(os.path.dirname(output_vocals_path))































    model = model or DEMUCS_MODEL































    segment = int(segment if segment is not None else DEMUCS_SEGMENT)































    shifts = int(shifts if shifts is not None else DEMUCS_SHIFTS)































    jobs = int(jobs if jobs is not None else DEMUCS_JOBS)































    cmd = [































        DEMUCS_PYTHON,































        DEMUCS_SCRIPT,































        input_audio_path,































        output_vocals_path,































        "-n",































        model,































        "-d",































        DEMUCS_DEVICE,































        "--segment",































        str(segment),































        "--shifts",































        str(shifts),































        "-j",































        str(jobs),































    ]































    run_cmd(cmd, timeout=DEMUCS_TIMEOUT)































































































def remux_vocals_video(input_video_path, vocals_audio_path, output_video_path):































    run_cmd([































        FFMPEG, "-y", "-i", input_video_path, "-i", vocals_audio_path, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", output_video_path,































    ], timeout=DEMUCS_TIMEOUT)
    if not valid_video_file(output_video_path):
        raise RuntimeError("no-BGM output is not a valid video: %s" % output_video_path)
    if not valid_av_duration_alignment(output_video_path):
        raise RuntimeError("no-BGM output has audio/video duration mismatch: %s" % output_video_path)































































































def is_memory_related_error(message):































    text = str(message or "").lower()































    keywords = [































        "can't allocate memory",































        "cannot allocate memory",































        "defaultcpuallocator",































        "out of memory",































        "killed",































    ]































    return any(keyword in text for keyword in keywords)































































































def is_demucs_recoverable_error(message):































    text = str(message or "").lower()































    keywords = [































        "invalid for input of size",































        "runtimeerror: shape",































        "shape '[",































        "tensor size mismatch",































        "not enough memory",































    ]































    return is_memory_related_error(message) or any(keyword in text for keyword in keywords)































































































def demucs_profiles():































    profiles = [































        {"model": DEMUCS_MODEL, "segment": DEMUCS_SEGMENT, "shifts": DEMUCS_SHIFTS, "jobs": DEMUCS_JOBS, "label": "%s/s%d" % (DEMUCS_MODEL, DEMUCS_SEGMENT)},































    ]































    fallback = {"model": DEMUCS_FALLBACK_MODEL, "segment": DEMUCS_FALLBACK_SEGMENT, "shifts": DEMUCS_FALLBACK_SHIFTS, "jobs": 0, "label": "%s/s%d" % (DEMUCS_FALLBACK_MODEL, DEMUCS_FALLBACK_SEGMENT)}
    if str(DEMUCS_DEVICE or "").lower() == "cpu" and fallback["model"]:
        return [fallback]































    if fallback["model"] and (fallback["model"] != profiles[0]["model"] or fallback["segment"] != profiles[0]["segment"]):































        profiles.append(fallback)































    return profiles































































































def get_wav_duration_seconds(path):































    with wave.open(path, "rb") as wav_file:































        frame_rate = wav_file.getframerate() or 1































        return float(wav_file.getnframes()) / float(frame_rate)































































































def split_wav_file(input_path, output_dir, chunk_seconds):































    ensure_dir(output_dir)































    chunk_paths = []































    with wave.open(input_path, "rb") as src:































        params = src.getparams()































        frame_rate = src.getframerate() or 1































        frames_per_chunk = max(frame_rate, int(chunk_seconds * frame_rate))































        index = 0































        while True:































            frames = src.readframes(frames_per_chunk)































            if not frames:































                break































            chunk_path = os.path.join(output_dir, "chunk_%03d.wav" % index)































            with wave.open(chunk_path, "wb") as dst:































                dst.setparams(params)































                dst.writeframes(frames)































            chunk_paths.append(chunk_path)































            index += 1































    return chunk_paths































































































def concat_wav_files(input_paths, output_path):































    if not input_paths:































        raise ValueError("no wav chunks to concatenate")































    with wave.open(input_paths[0], "rb") as first:































        params = first.getparams()































        first_frames = first.readframes(first.getnframes())































    with wave.open(output_path, "wb") as out:































        out.setparams(params)































        out.writeframes(first_frames)































        for path in input_paths[1:]:































            with wave.open(path, "rb") as wav_file:































                out.writeframes(wav_file.readframes(wav_file.getnframes()))































































































def run_no_bgm_pipeline(job, source_video_path, output_video_path, public_output_path):































    no_bgm_dir = os.path.join(WORK_ROOT, job["job_id"], "no_bgm")































    cache_dir = os.path.join(no_bgm_dir, "cache")































    stems_dir = os.path.join(no_bgm_dir, "stems")































    ensure_dir(cache_dir)































    ensure_dir(stems_dir)































    audio_wav = os.path.join(cache_dir, "full.wav")































    vocals_wav = os.path.join(stems_dir, "vocals.wav")































































    update_no_bgm_stage(job, 86, "开始提取原视频音轨")































    ensure_job_not_deleted(job["job_id"])































    extract_audio_for_demucs(source_video_path, audio_wav)































































    duration_seconds = get_wav_duration_seconds(audio_wav)































    chunk_attempts = [































        {"chunk_seconds": max(int(DEMUCS_CHUNK_SECONDS), 30), "label": "标准分段"},































        {"chunk_seconds": max(min(int(DEMUCS_CHUNK_SECONDS), 60), 24), "label": "兼容分段"},































        {"chunk_seconds": max(int(DEMUCS_FALLBACK_CHUNK_SECONDS), 20), "label": "低内存分段"},































        {"chunk_seconds": 24, "label": "极限分段"},































    ]































    profiles = demucs_profiles()































    last_error = None































































    with DEMUCS_LOCK:































        for chunk_index, chunk_plan in enumerate(chunk_attempts, 1):































            chunk_seconds = int(chunk_plan["chunk_seconds"])































            chunk_root = os.path.join(cache_dir, "chunks_%ss" % chunk_seconds)































            chunk_output_root = os.path.join(stems_dir, "chunks_%ss" % chunk_seconds)































            if os.path.isdir(chunk_root):































                shutil.rmtree(chunk_root, ignore_errors=True)































            if os.path.isdir(chunk_output_root):































                shutil.rmtree(chunk_output_root, ignore_errors=True)































            ensure_dir(chunk_root)































            ensure_dir(chunk_output_root)































            chunk_paths = split_wav_file(audio_wav, chunk_root, chunk_seconds)































            if not chunk_paths:































                chunk_paths = [audio_wav]































































            total_chunks = len(chunk_paths)































            try:































                for part_index, chunk_path in enumerate(chunk_paths, 1):































                    ensure_job_not_deleted(job["job_id"])































                    chunk_vocals_path = os.path.join(chunk_output_root, "vocals_%03d.wav" % (part_index - 1))































                    chunk_success = False































                    for profile_index, profile in enumerate(profiles, 1):































                        try:































                            percent = 90 if total_chunks <= 1 else min(94, 88 + int((part_index - 1) * 6 / max(total_chunks, 1)))































                            update_no_bgm_stage(































                                job,































                                percent,































                                "人声分离中（%s，%s，第 %d/%d 段，配置 %d/%d）"































                                % (































                                    chunk_plan["label"],































                                    profile["label"],































                                    part_index,































                                    total_chunks,































                                    profile_index,































                                    len(profiles),































                                ),































                            )































                            extract_vocals_with_demucs(































                                chunk_path,































                                chunk_vocals_path,































                                model=profile["model"],































                                segment=profile["segment"],































                                shifts=profile["shifts"],































                                jobs=profile["jobs"],































                            )































                            chunk_success = True































                            break































                        except Exception as exc:































                            last_error = exc































                            if profile_index < len(profiles) and is_demucs_recoverable_error(exc):































                                logging.warning(































                                    "demucs recoverable retry for job %s chunk %s with fallback profile %s",































                                    job["job_id"],































                                    os.path.basename(chunk_path),































                                    profiles[profile_index]["label"],































                                )































                                update_no_bgm_stage(































                                    job,































                                    max(89, percent - 1),































                                    "Demucs 重试中，切换到 %s" % profiles[profile_index]["label"],































                                )































                                continue































                            raise































                    if not chunk_success:































                        raise last_error or RuntimeError("demucs chunk process failed")































































                concat_wav_files(































                    [os.path.join(chunk_output_root, "vocals_%03d.wav" % index) for index in range(total_chunks)],































                    vocals_wav,































                )































                update_no_bgm_stage(job, 95, "人声分离完成，开始封装去 BGM 视频")































                ensure_job_not_deleted(job["job_id"])































                remux_vocals_video(source_video_path, vocals_wav, output_video_path)































                ensure_job_not_deleted(job["job_id"])































                shutil.copy2(output_video_path, public_output_path)































                job["output_video_no_bgm_url"] = publish_asset(public_output_path)





























                update_no_bgm_stage(job, 98, "去 BGM 成片已上传")































                return































            except Exception as exc:































                last_error = exc































                if chunk_index < len(chunk_attempts) and is_demucs_recoverable_error(exc):































                    logging.warning(































                        "demucs chunk retry for job %s from %ss to %ss due to recoverable error",































                        job["job_id"],































                        chunk_seconds,































                        chunk_attempts[chunk_index]["chunk_seconds"],































                    )































                    update_no_bgm_stage(































                        job,































                        88,































                        "Demucs 重试中，切换到 %s（约 %.0f 秒音频）"































                        % (chunk_attempts[chunk_index]["label"], duration_seconds),































                    )































                    continue































                raise































































    raise last_error or RuntimeError("demucs no-bgm pipeline failed")































































































def generate_no_bgm_video(source_video_path, output_video_path, job_id):































    ensure_dir(CODEX_MEDIA_WORKSPACE)































    no_bgm_dir = os.path.join(WORK_ROOT, job_id, "no_bgm")































    cache_dir = os.path.join(no_bgm_dir, "cache")































    stems_dir = os.path.join(no_bgm_dir, "stems")































    ensure_dir(cache_dir)































    ensure_dir(stems_dir)































    audio_wav = os.path.join(cache_dir, "full.wav")































    vocals_wav = os.path.join(stems_dir, "vocals.wav")































    extract_audio_for_demucs(source_video_path, audio_wav)































    extract_vocals_with_demucs(audio_wav, vocals_wav)































    remux_vocals_video(source_video_path, vocals_wav, output_video_path)































































































def should_auto_retry_job(exc):































    text = str(exc or "").lower()































    if "job deleted" in text:































        return False































    if is_screenshot_generation_no_output_error(exc):

        return False

    if is_screenshot_batch_recoverable_error(exc):

        return True

    retry_keywords = [































        "服务重启，任务已按断点恢复",































        "timed out",































        "connection reset",































        "broken pipe",































        "codex cover service error",































        "502",































        "503",































        "504",































    ]































    return is_memory_related_error(exc) or any(keyword in text for keyword in retry_keywords)































































































def ensure_job_not_deleted(job_id):































    if is_job_deleted(job_id):































        raise RuntimeError("job deleted")































































































def update_download_stage(job, current_index, total_files, file_label, downloaded, total_bytes):































    total_files = max(1, total_files)































    file_ratio = float(downloaded) / float(total_bytes) if total_bytes and total_bytes > 0 else 0.0































    overall_ratio = min(1.0, (float(current_index) + file_ratio) / float(total_files))































    progress = 8 + (30 - 8) * overall_ratio































    if total_bytes and total_bytes > 0:































        size_mb = total_bytes / 1024.0 / 1024.0































        done_mb = downloaded / 1024.0 / 1024.0































        detail = "%s %.1f/%.1f MB" % (file_label, done_mb, size_mb)































    else:































        detail = "%s 下载中" % file_label































    set_job_progress(job, status="downloading", progress=progress, detail=detail)































































































def update_render_stage(job, completed_steps, total_steps, detail):































    total_steps = max(1, total_steps)































    overall_ratio = min(1.0, float(completed_steps) / float(total_steps))































    progress = 46 + (82 - 46) * overall_ratio































    set_job_progress(job, status="rendering", progress=progress, detail=detail)































































































def update_no_bgm_stage(job, progress, detail):































    set_job_progress(job, status="removing_bgm", progress=progress, detail=detail)


def gpu_video_worker_enabled():
    return bool(GPU_VIDEO_WORKER_URL)


def call_gpu_video_worker(job, requested, outputs, await_cover_16x9=False):
    if not gpu_video_worker_enabled():
        return None
    if not GPU_VIDEO_WORKER_TOKEN:
        raise ValueError("GPU_VIDEO_WORKER_TOKEN is required when GPU_VIDEO_WORKER_URL is set")
    payload = {
        "job_id": job["job_id"],
        "content_id": job.get("content_id", ""),
        "episode_start": job.get("episode_start", 0),
        "episode_end": job.get("episode_end", 0),
        "outputs": {
            "concat_video": bool(outputs.get("concat_video", True)),
            "no_bgm_video": bool(outputs.get("no_bgm_video", True)),
        },
        "cover_16x9_url": str(job.get("_gpu_cover_16x9_url") or job.get("cover_16x9_url") or ""),
        "await_cover_16x9": bool(await_cover_16x9),
        "episodes": [
            {
                "episode_number": int(item["episode_number"]),
                "episode_url": item["episode_url"],
            }
            for item in requested
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % GPU_VIDEO_WORKER_TOKEN,
    }
    response = requests.post(
        GPU_VIDEO_WORKER_URL + "/api/gpu-video/render",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        timeout=GPU_VIDEO_WORKER_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError("GPU video worker failed (%s): %s" % (response.status_code, response.text[:2000]))
    result = response.json()
    if result.get("error"):
        raise RuntimeError(result.get("error"))
    return result


def submit_gpu_video_cover(job, cover_16x9_url):
    if not gpu_video_worker_enabled() or not cover_16x9_url:
        return None
    if not GPU_VIDEO_WORKER_TOKEN:
        raise ValueError("GPU_VIDEO_WORKER_TOKEN is required when GPU_VIDEO_WORKER_URL is set")
    payload = {
        "job_id": job["job_id"],
        "cover_16x9_url": cover_16x9_url,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % GPU_VIDEO_WORKER_TOKEN,
    }
    response = requests.post(
        GPU_VIDEO_WORKER_URL + "/api/gpu-video/cover",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("error"):
        raise RuntimeError(result.get("error"))
    return result


def gpu_cover_url_marker_path(workdir):
    return os.path.join(workdir, "cover_16x9_url.txt")


def write_gpu_cover_url(workdir, cover_16x9_url):
    ensure_dir(workdir)
    marker_path = gpu_cover_url_marker_path(workdir)
    tmp_path = marker_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fp:
        fp.write(str(cover_16x9_url or "").strip())
    os.replace(tmp_path, marker_path)


def wait_for_gpu_cover_url(workdir, timeout_seconds):
    marker_path = gpu_cover_url_marker_path(workdir)
    deadline = time.time() + max(1, int(timeout_seconds or 1))
    while time.time() < deadline:
        if os.path.isfile(marker_path):
            with open(marker_path, "r", encoding="utf-8") as fp:
                cover_url = fp.read().strip()
            if cover_url:
                return cover_url
        time.sleep(2)
    raise TimeoutError("timed out waiting for GPU cover url")


def gpu_video_result_path(job_id):
    safe_job_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(job_id or "").strip())
    return os.path.join(GPU_VIDEO_RESULT_ROOT, safe_job_id + ".json")


def gpu_video_result_satisfies_outputs(result, outputs):
    if not result:
        return False
    if bool(outputs.get("concat_video", True)):
        url = str(result.get("output_video_url") or "").strip()
        if not url or not public_artifact_ready(url, 1024 * 1024):
            return False
    if bool(outputs.get("no_bgm_video", True)):
        url = str(result.get("output_video_no_bgm_url") or "").strip()
        if not url or not public_artifact_ready(url, 1024 * 1024):
            return False
    return True


def read_gpu_video_result(job_id, outputs):
    result_path = gpu_video_result_path(job_id)
    if os.path.isfile(result_path):
        try:
            with open(result_path, "r", encoding="utf-8") as fp:
                result = json.load(fp)
            if gpu_video_result_satisfies_outputs(result, outputs):
                return result
        except Exception as exc:
            logging.warning("failed to read GPU result manifest: %s %s", result_path, exc)

    result = {"job_id": job_id, "output_video_url": "", "output_video_no_bgm_url": ""}
    if bool(outputs.get("concat_video", True)):
        result["output_video_url"] = build_drama_public_url(job_id, "material.mp4")
    if bool(outputs.get("no_bgm_video", True)):
        result["output_video_no_bgm_url"] = build_drama_public_url(job_id, "material_no_bgm.mp4")
    if gpu_video_result_satisfies_outputs(result, outputs):
        write_gpu_video_result(job_id, result)
        return result
    return None


def write_gpu_video_result(job_id, result):
    ensure_dir(GPU_VIDEO_RESULT_ROOT)
    result_path = gpu_video_result_path(job_id)
    tmp_path = result_path + ".tmp"
    payload = dict(result or {})
    payload["job_id"] = str(job_id or payload.get("job_id") or "")
    payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(tmp_path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, result_path)


def handle_gpu_video_cover(payload):
    if not GPU_VIDEO_WORKER_TOKEN:
        raise PermissionError("GPU_VIDEO_WORKER_TOKEN is not configured")
    job_id = str(payload.get("job_id", "") or "").strip()
    cover_16x9_url = str(payload.get("cover_16x9_url") or payload.get("cover_url") or "").strip()
    if not job_id:
        raise ValueError("missing job_id")
    if not cover_16x9_url:
        raise ValueError("missing cover_16x9_url")
    workdir = os.path.join(WORK_ROOT, job_id)
    write_gpu_cover_url(workdir, cover_16x9_url)
    return {"job_id": job_id, "ok": True}


def cleanup_gpu_video_job_files(job_id, workdir, public_dir):
    if not cos_enabled():
        logging.info("skip GPU local cleanup because COS is not enabled: job=%s", job_id)
        return

    def remove_job_dir(base_dir, target_dir, label):
        base_real = os.path.realpath(base_dir)
        target_real = os.path.realpath(target_dir)
        if not target_real.startswith(base_real + os.sep):
            logging.warning("skip GPU cleanup outside %s: %s", label, target_dir)
            return
        if os.path.basename(target_real) != str(job_id):
            logging.warning("skip GPU cleanup unexpected %s basename: %s", label, target_dir)
            return
        if os.path.isdir(target_real):
            shutil.rmtree(target_real, ignore_errors=True)
            logging.info("cleaned GPU %s dir after COS upload: %s", label, target_real)

    remove_job_dir(WORK_ROOT, workdir, "work")
    remove_job_dir(PUBLIC_ROOT, public_dir, "public")


def handle_gpu_video_render(payload):
    job_id = str((payload or {}).get("job_id", "") or "").strip()
    if not job_id:
        raise ValueError("missing job_id")
    lock = get_named_runtime_lock(GPU_VIDEO_RENDER_LOCKS, GPU_VIDEO_RENDER_LOCKS_LOCK, job_id)
    with lock:
        return _handle_gpu_video_render_unlocked(payload)


def _handle_gpu_video_render_unlocked(payload):
    if not GPU_VIDEO_WORKER_TOKEN:
        raise PermissionError("GPU_VIDEO_WORKER_TOKEN is not configured")
    job_id = str(payload.get("job_id", "") or "").strip()
    if not job_id:
        raise ValueError("missing job_id")
    episodes = payload.get("episodes") or []
    if not episodes:
        raise ValueError("missing episodes")
    outputs = payload.get("outputs") or {}
    cover_16x9_url = str(payload.get("cover_16x9_url") or payload.get("cover_url") or "").strip()
    await_cover_16x9 = bool(payload.get("await_cover_16x9") or payload.get("wait_for_cover"))
    cover_wait_timeout = int(payload.get("cover_wait_timeout") or GPU_VIDEO_WORKER_TIMEOUT or 1800)
    render_concat = bool(outputs.get("concat_video", True) or outputs.get("no_bgm_video", True))
    render_no_bgm = bool(outputs.get("no_bgm_video", True))
    publish_concat = bool(outputs.get("concat_video", True))
    if not render_concat:
        return {"job_id": job_id, "output_video_url": "", "output_video_no_bgm_url": ""}

    existing_result = read_gpu_video_result(job_id, outputs)
    if existing_result:
        logging.info("reuse GPU video result for job=%s", job_id)
        return existing_result

    workdir = os.path.join(WORK_ROOT, job_id)
    download_dir = os.path.join(workdir, "downloads")
    segment_dir = os.path.join(workdir, "segments")
    concat_segment_dir = os.path.join(workdir, "concat_segments")
    public_dir = os.path.join(PUBLIC_ROOT, job_id)
    ensure_dir(download_dir)
    ensure_dir(segment_dir)
    ensure_dir(concat_segment_dir)
    ensure_dir(public_dir)

    job = {
        "_gpu_worker": True,
        "job_id": job_id,
        "content_id": str(payload.get("content_id", "") or ""),
        "episode_start": int(payload.get("episode_start") or 0),
        "episode_end": int(payload.get("episode_end") or 0),
        "status": "rendering",
        "progress": 0,
        "progress_detail": "",
        "output_video_url": "",
        "output_video_no_bgm_url": "",
    }
    segment_paths = []
    total_steps = (
        (1 if (cover_16x9_url or await_cover_16x9) else 0)
        + max(1, len(episodes))
        + 1
        + (1 if render_no_bgm else 0)
        + (1 if publish_concat else 0)
    )
    completed_steps = 0

    if cover_16x9_url:
        write_gpu_cover_url(workdir, cover_16x9_url)

    episode_work_items = []
    for item in episodes:
        episode_number = int(item.get("episode_number") or 0)
        episode_url = str(item.get("episode_url", "") or "").strip()
        if not episode_url:
            raise ValueError("episode %s missing episode_url" % episode_number)
        episode_work_items.append({
            "episode_number": episode_number,
            "episode_url": episode_url,
            "source_path": os.path.join(download_dir, "%03d.mp4" % episode_number),
            "normalized_path": os.path.join(segment_dir, "%03d.mp4" % episode_number),
        })

    max_download_workers = max(1, min(4, len(episode_work_items)))
    download_futures = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_download_workers) as download_executor:
        for item in episode_work_items:
            if file_ready(item["source_path"]):
                continue
            download_futures[item["episode_number"]] = download_executor.submit(
                download_file,
                item["episode_url"],
                item["source_path"],
            )
        if download_futures:
            logging.info(
                "GPU prefetch queued %d episode downloads with %d workers for job=%s",
                len(download_futures),
                max_download_workers,
                job_id,
            )

        for item in episode_work_items:
            future = download_futures.get(item["episode_number"])
            if future is not None:
                future.result()
            segment_paths.append(item["source_path"])
            completed_steps += 1
            update_render_stage(job, completed_steps, total_steps, "GPU episode %d downloaded" % item["episode_number"])

    if cover_16x9_url or await_cover_16x9:
        if not cover_16x9_url:
            cover_16x9_url = wait_for_gpu_cover_url(workdir, cover_wait_timeout)
        cover_path = os.path.join(download_dir, "cover_16x9.jpg")
        intro_path = os.path.join(segment_dir, "000_intro.mp4")
        remove_invalid_video_file(intro_path, "GPU intro")
        if not file_ready(cover_path):
            download_file(cover_16x9_url, cover_path)
        if not file_ready(intro_path):
            reference_path = episode_work_items[0]["source_path"] if episode_work_items else None
            render_intro(cover_path, intro_path, reference_path=reference_path)
        segment_paths.insert(0, intro_path)
        completed_steps += 1
        update_render_stage(job, completed_steps, total_steps, "GPU intro rendered")

    segment_paths = prepare_concat_segments(segment_paths, concat_segment_dir)

    output_name = "%s_%s_eps_%s_%s.mp4" % (
        job["content_id"] or "material",
        job_id[:8],
        job["episode_start"] or "start",
        job["episode_end"] or "end",
    )
    output_path = os.path.join(workdir, output_name)
    public_video_path = os.path.join(public_dir, "material.mp4")
    remove_invalid_video_file(output_path, "GPU concat workspace")
    remove_invalid_video_file(public_video_path, "GPU concat public")
    if not file_ready(output_path):
        concat_segments(segment_paths, output_path)
    if not valid_video_file(output_path):
        raise RuntimeError("GPU concat video is invalid: %s" % output_path)
    if publish_concat and not file_ready(public_video_path):
        shutil.copy2(output_path, public_video_path)
    if publish_concat and not valid_video_file(public_video_path):
        raise RuntimeError("GPU concat video is invalid: %s" % public_video_path)
    update_render_stage(job, completed_steps, total_steps, "GPU concat video ready")

    if render_no_bgm:
        no_bgm_output_path = os.path.join(workdir, "material_no_bgm.mp4")
        public_no_bgm_path = os.path.join(public_dir, "material_no_bgm.mp4")
        remove_invalid_video_file(no_bgm_output_path, "GPU no-BGM workspace")
        remove_invalid_video_file(public_no_bgm_path, "GPU no-BGM public")
        if file_ready(public_no_bgm_path):
            job["output_video_no_bgm_url"] = publish_asset(public_no_bgm_path)
        else:
            run_no_bgm_pipeline(job, output_path, no_bgm_output_path, public_no_bgm_path)
        completed_steps += 1
        update_render_stage(job, completed_steps, total_steps, "GPU no-BGM video uploaded")

    if publish_concat:
        job["output_video_url"] = publish_asset(public_video_path)
        completed_steps += 1
        update_render_stage(job, completed_steps, total_steps, "GPU concat video uploaded")

    result = {
        "job_id": job_id,
        "output_video_url": job.get("output_video_url", ""),
        "output_video_no_bgm_url": job.get("output_video_no_bgm_url", ""),
    }
    if publish_concat and not result["output_video_url"]:
        raise RuntimeError("GPU concat video upload did not return a URL")
    if render_no_bgm and not result["output_video_no_bgm_url"]:
        raise RuntimeError("GPU no-BGM video upload did not return a URL")
    write_gpu_video_result(job_id, result)
    cleanup_gpu_video_job_files(job_id, workdir, public_dir)
    return result































































































def submit_job(payload, actor_session=None):















    app_id = str(payload.get("app_id", "")).strip()































    content_id = str(payload.get("content_id", "")).strip()































    episode_start = int(payload.get("episode_start", 0))































    episode_end = int(payload.get("episode_end", 0))































    if not app_id:































        raise ValueError("app_id 不能为空")































    if not content_id:































        raise ValueError("content_id 不能为空")































    if episode_start <= 0 or episode_end <= 0:































        raise ValueError("episode_start 和 episode_end 必须大于 0")































    if episode_start > episode_end:































        raise ValueError("episode_start 不能大于 episode_end")































    outputs = normalize_outputs(payload.get("outputs", {}))































    advanced = normalize_advanced_options(payload.get("advanced_options", {}))































    ensure_no_duplicate_job(app_id, content_id)































    validation = validate_content_request(app_id, content_id, episode_start, episode_end)































    job = {































        "job_id": uuid.uuid4().hex,































        "app_id": validation["app_id"],































        "content_id": content_id,































        "episode_start": episode_start,































        "episode_end": episode_end,































        "total_episodes": validation["total_episodes"],































        "cover_source_url": validation["cover_source_url"],































        "cover_16x9_url": "",































        "output_video_url": "",































        "output_video_no_bgm_url": "",































        "outputs": outputs,































        "advanced_options": advanced,































        "status": "queued",































        "progress": 2,















        "progress_detail": "任务已进入队列",















        "error_message": "",















        "creator_user_id": (actor_session or {}).get("user_id", ""),















        "creator_open_id": (actor_session or {}).get("open_id", ""),















        "creator_name": (actor_session or {}).get("name", ""),















        "completion_notified_at": "",















        "completion_notification_error": "",

        "finished_at": "",















        "app": validation["app"],















        "country": validation["country"],















        "language": validation["language"],















        "drama_name": validation["drama_name"],































    }































    clear_job_deleted_marker(job["job_id"])































    upsert_job_record(job)































    run_job_async(job)































    return {































        "message": "job accepted",































        "job_id": job["job_id"],































        "status": job["status"],































        "content_available": True,































        "app_id": validation["app_id"],































        "drama_name": validation["drama_name"],































        "app": validation["app"],































        "country": validation["country"],































        "language": validation["language"],































        "total_episodes": validation["total_episodes"],































        "available_episode_start": validation["available_episode_start"],































        "available_episode_end": validation["available_episode_end"],































        "created_at": now_text(),































    }































































































def process_job(job):































    clear_job_deleted_marker(job["job_id"])
    if reconcile_job_outputs_from_public_artifacts(job, persist=True, notify=True):
        return































    outputs = normalize_outputs(job.get("outputs", {}))































    workdir = os.path.join(WORK_ROOT, job["job_id"])































    download_dir = os.path.join(workdir, "downloads")































    normalized_dir = os.path.join(workdir, "normalized")































    public_dir = os.path.join(PUBLIC_ROOT, job["job_id"])































    ensure_dir(download_dir)































    ensure_dir(normalized_dir)































    ensure_dir(public_dir)































































    set_job_progress(job, status="validating", progress=6, detail="校验剧集与资源可用性")































    ensure_job_not_deleted(job["job_id"])































    validation = validate_content_request(job["app_id"], job["content_id"], job["episode_start"], job["episode_end"])































    requested = validation["requested"]































    cover_url = validation["cover_source_url"]































    job["app"] = validation["app"]































    job["country"] = validation["country"]































    job["language"] = validation["language"]































    job["drama_name"] = validation["drama_name"]































    job["total_episodes"] = validation["total_episodes"]































    job["cover_source_url"] = cover_url































































    need_video_pipeline = outputs["concat_video"] or outputs["no_bgm_video"]































    need_cover = outputs["cover_16x9"] or (need_video_pipeline and not gpu_video_worker_enabled())































































    cover_source_path = os.path.join(download_dir, "cover_source.jpg")































    total_download_files = len(requested) + (1 if need_cover else 0)































    download_file_index = 0
    gpu_executor = None
    gpu_future = None































    if need_cover:































        if file_ready(cover_source_path):































            set_job_progress(job, status="downloading", progress=8, detail="复用已下载的封面原图")































        else:































            set_job_progress(job, status="downloading", progress=8, detail="开始下载封面原图")































            ensure_job_not_deleted(job["job_id"])































            download_file(































                cover_url,































                cover_source_path,































                progress_callback=lambda downloaded, total: update_download_stage(































                    job,































                    download_file_index,































                    total_download_files,































                    "封面原图",































                    downloaded,































                    total,































                ),































            )































        download_file_index += 1































        ensure_job_not_deleted(job["job_id"])































































    downloaded_episode_paths = []































    if need_video_pipeline and not gpu_video_worker_enabled():































        for episode in requested:































            filename = "%03d.mp4" % episode["episode_number"]































            local_path = os.path.join(download_dir, filename)































            label = "第 %d 集" % episode["episode_number"]































            current_index = download_file_index































            if file_ready(local_path):































                update_download_stage(job, current_index, total_download_files, label, 1, 1)































            else:































                download_file(































                    episode["episode_url"],































                    local_path,































                    progress_callback=lambda downloaded, total, idx=current_index, title=label: update_download_stage(































                        job,































                        idx,































                        total_download_files,































                        title,































                        downloaded,































                        total,































                    ),































                )































            downloaded_episode_paths.append((episode["episode_number"], local_path))































            download_file_index += 1































            ensure_job_not_deleted(job["job_id"])































































    cover_16x9_path = os.path.join(workdir, "cover_16x9.jpg")































    public_cover_path = os.path.join(public_dir, "cover_16x9.jpg")































    public_cover_url = build_public_url(public_cover_path)

    if need_video_pipeline and gpu_video_worker_enabled():
        set_job_progress(job, status="rendering", progress=20, detail="GPU 服已开始处理素材，等待封面后合并")
        ensure_job_not_deleted(job["job_id"])
        gpu_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        gpu_future = gpu_executor.submit(call_gpu_video_worker, job, requested, outputs, need_cover)































    if need_cover:































        if file_ready(public_cover_path):































            if not file_ready(cover_16x9_path):































                shutil.copy2(public_cover_path, cover_16x9_path)































            if outputs["cover_16x9"]:































                job["cover_16x9_url"] = publish_asset(public_cover_path)





























            set_job_progress(job, status="processing_cover", progress=44, detail="复用已有 16:9 封面")































        else:































            set_job_progress(job, status="processing_cover", progress=34, detail="提交 16:9 封面生成任务")































            ensure_job_not_deleted(job["job_id"])































            cover_result = generate_cover_via_codex_service(job, cover_source_path, cover_16x9_path, public_cover_path)































            ensure_job_not_deleted(job["job_id"])































            cover_16x9_path = cover_result.get("workspace_output_path") or cover_16x9_path































            if outputs["cover_16x9"]:































                job["cover_16x9_url"] = publish_asset(public_cover_path)





























            set_job_progress(job, status="processing_cover", progress=44, detail="16:9 封面已生成")































































    if need_video_pipeline and gpu_video_worker_enabled() and outputs["cover_16x9"] and os.path.isfile(public_cover_path):
        job["_gpu_cover_16x9_url"] = job.get("cover_16x9_url") or publish_asset(public_cover_path)
        if outputs["cover_16x9"] and not job.get("cover_16x9_url"):
            job["cover_16x9_url"] = job["_gpu_cover_16x9_url"]
        submit_gpu_video_cover(job, job["_gpu_cover_16x9_url"])

    if need_video_pipeline and gpu_video_worker_enabled():
        set_job_progress(job, status="rendering", progress=46, detail="已提交 GPU 服制作合集视频")
        ensure_job_not_deleted(job["job_id"])
        if gpu_future is None:
            gpu_result = call_gpu_video_worker(job, requested, outputs)
        else:
            gpu_result = gpu_future.result(timeout=GPU_VIDEO_WORKER_TIMEOUT + 120)
            gpu_executor.shutdown(wait=False)
            gpu_executor = None
        if outputs["concat_video"]:
            job["output_video_url"] = gpu_result.get("output_video_url", "")
        if outputs["no_bgm_video"]:
            job["output_video_no_bgm_url"] = gpu_result.get("output_video_no_bgm_url", "")
        if outputs["cover_16x9"] and not job.get("cover_16x9_url") and os.path.isfile(public_cover_path):
            job["cover_16x9_url"] = publish_asset(public_cover_path)
        set_job_progress(job, status="rendering", progress=98, detail="GPU 服视频制作完成")
        need_video_pipeline = False

    if need_video_pipeline:































        total_render_steps = len(downloaded_episode_paths) + 1 + (1 if need_cover else 0)































        completed_render_steps = 0































        update_render_stage(job, completed_render_steps, total_render_steps, "开始生成片头")































        intro_path = os.path.join(normalized_dir, "000_intro.mp4")
        remove_invalid_video_file(intro_path, "CPU intro")































        if file_ready(intro_path):































            update_render_stage(job, completed_render_steps, total_render_steps, "复用已有片头")































        else:































            ensure_job_not_deleted(job["job_id"])































            render_intro(cover_16x9_path, intro_path)































        completed_render_steps += 1































        update_render_stage(job, completed_render_steps, total_render_steps, "片头已生成")































        segment_paths = [intro_path]































        for episode_number, source_path in downloaded_episode_paths:































            normalized_path = os.path.join(normalized_dir, "%03d.mp4" % episode_number)































            update_render_stage(































                job,































                completed_render_steps,































                total_render_steps,































                "正在处理第 %d 集画面与音频" % episode_number,































            )































            if file_ready(normalized_path):































                update_render_stage(































                    job,































                    completed_render_steps,































                    total_render_steps,































                    "复用第 %d 集已标准化片段" % episode_number,































                )































            else:































                ensure_job_not_deleted(job["job_id"])































                normalize_episode(source_path, normalized_path)































            segment_paths.append(normalized_path)































            completed_render_steps += 1































            update_render_stage(































                job,































                completed_render_steps,































                total_render_steps,































                "第 %d 集已完成标准化" % episode_number,































            )































        output_name = "%s_%s_eps_%d_%d.mp4" % (job["content_id"], job["job_id"][:8], job["episode_start"], job["episode_end"])































        output_path = os.path.join(workdir, output_name)































        public_video_path = os.path.join(public_dir, "material.mp4")































        public_video_url = publish_asset(public_video_path) if file_ready(public_video_path) else build_public_url(public_video_path)





























        update_render_stage(job, completed_render_steps, total_render_steps, "正在拼接合集视频")































        if file_ready(output_path):































            update_render_stage(job, completed_render_steps, total_render_steps, "复用已有合集视频")































        elif file_ready(public_video_path):































            shutil.copy2(public_video_path, output_path)































            update_render_stage(job, completed_render_steps, total_render_steps, "复用已上传的合集视频")































        else:































            ensure_job_not_deleted(job["job_id"])































            concat_segments(segment_paths, output_path)































        completed_render_steps += 1































        update_render_stage(job, completed_render_steps, total_render_steps, "合集视频已生成")































































        if not file_ready(public_video_path):































            ensure_job_not_deleted(job["job_id"])































            shutil.copy2(output_path, public_video_path)































        if outputs["concat_video"]:































            job["output_video_url"] = publish_asset(public_video_path)





























        if outputs["cover_16x9"] and not job.get("cover_16x9_url") and os.path.isfile(public_cover_path):































            job["cover_16x9_url"] = publish_asset(public_cover_path)





























        set_job_progress(job, status="rendering", progress=82, detail="合集视频已上传")































































        if outputs["no_bgm_video"]:































            no_bgm_output_path = os.path.join(workdir, "material_no_bgm.mp4")































            public_no_bgm_path = os.path.join(public_dir, "material_no_bgm.mp4")































            if file_ready(public_no_bgm_path):































                if not file_ready(no_bgm_output_path):































                    shutil.copy2(public_no_bgm_path, no_bgm_output_path)































                job["output_video_no_bgm_url"] = publish_asset(public_no_bgm_path)





























                update_no_bgm_stage(job, 98, "复用已有去 BGM 成片")































            else:































                run_no_bgm_pipeline(job, output_path, no_bgm_output_path, public_no_bgm_path)































































    job["status"] = "done"































    job["progress"] = 100































    job["progress_detail"] = "全部产物已生成"















    job["error_message"] = ""















    upsert_job_record(job)















    notify_job_creator_on_completion(job)















































































def resume_failed_no_bgm_job(job):































    output_video_path = public_url_to_path(job.get("output_video_url", ""))

    if not output_video_path:

        candidate_path = os.path.join(PUBLIC_ROOT, job["job_id"], "material.mp4")

        if os.path.isfile(candidate_path):

            output_video_path = candidate_path





























    if not output_video_path or not os.path.isfile(output_video_path):































        raise ValueError("failed job has no reusable output_video")































    workdir = os.path.join(WORK_ROOT, job["job_id"])































    public_dir = os.path.join(PUBLIC_ROOT, job["job_id"])































    ensure_dir(workdir)































    ensure_dir(public_dir)































    no_bgm_output_path = os.path.join(workdir, "material_no_bgm.mp4")































    public_no_bgm_path = os.path.join(public_dir, "material_no_bgm.mp4")































    job["status"] = "rendering"































    job["error_message"] = ""







    job["completion_notified_at"] = ""







    job["completion_notification_error"] = ""







    job["progress_detail"] = "复用已有合集视频，重新生成去 BGM 版本"































    upsert_job_record(job)































































    def target():































        attempts = max(1, JOB_AUTO_RETRY_ATTEMPTS + 1)































        for attempt in range(1, attempts + 1):































            try:































                if attempt > 1:































                    set_job_progress(































                        job,































                        status="rendering",































                        progress=82,































                        detail="去 BGM 失败，开始自动重试（第 %d/%d 次）" % (attempt, attempts),































                    )































                run_no_bgm_pipeline(job, output_video_path, no_bgm_output_path, public_no_bgm_path)































                job["status"] = "done"































                job["progress"] = 100































                job["progress_detail"] = "全部产物已生成"















                job["error_message"] = ""















                upsert_job_record(job)















                notify_job_creator_on_completion(job)















                return















            except Exception as exc:































                logging.exception("failed no-bgm recovery: %s", job["job_id"])































                if attempt < attempts and should_auto_retry_job(exc):































                    continue































                job["status"] = "failed"































                job["progress"] = clamp_progress(job.get("progress", 0))































                message = str(exc).strip() or exc.__class__.__name__































                trace = traceback.format_exc(limit=8)































                job["error_message"] = "%s\n%s" % (message, trace)































                upsert_job_record(job)















                notify_job_creator_on_completion(job)































                return































































    thread = threading.Thread(target=target, name="job-recover-%s" % job["job_id"][:8])































    thread.daemon = True































    thread.start()































































































def run_job_async(job):

    if DRAMA_JOB_USE_WORKER:

        logging.info("drama job queued for external worker: %s", job.get("job_id"))

        return































    def target():































        attempts = max(1, JOB_AUTO_RETRY_ATTEMPTS + 1)































        for attempt in range(1, attempts + 1):































            try:































                if attempt > 1:































                    set_job_progress(































                        job,































                        status="queued",































                        progress=max(2, clamp_progress(job.get("progress", 0))),































                        detail="任务失败，开始自动重试（第 %d/%d 次）" % (attempt, attempts),































                    )































                process_job(job)































                return































            except Exception as exc:































                logging.exception("job failed: %s", job["job_id"])































                if is_job_deleted(job["job_id"]) and str(exc).strip() == "job deleted":































                    return































                if attempt < attempts and should_auto_retry_job(exc):































                    continue































                job["status"] = "failed"































                job["progress"] = clamp_progress(job.get("progress", 0))































                message = str(exc).strip() or exc.__class__.__name__































                trace = traceback.format_exc(limit=8)































                job["error_message"] = "%s\n%s" % (message, trace)































                upsert_job_record(job)















                notify_job_creator_on_completion(job)































                return































    thread = threading.Thread(target=target, name="job-%s" % job["job_id"][:8])































    thread.daemon = True































    thread.start()































































































def delete_job(job_id):































    job = fetch_job_row(job_id)































    if not job:































        return False































    mark_job_deleted(job_id)































    with JOB_DB_LOCK:































        conn = get_job_db_connection()































        try:































            conn.execute("DELETE FROM drama_material_job WHERE job_id = ?", (job_id,))































            conn.commit()































        finally:































            conn.close()































    shutil.rmtree(os.path.join(WORK_ROOT, job_id), ignore_errors=True)































    shutil.rmtree(os.path.join(PUBLIC_ROOT, job_id), ignore_errors=True)































    shutil.rmtree(os.path.join("/root/codex_cover_jobs", job_id), ignore_errors=True)































    return True































































































def resume_job_from_checkpoint(job):































    clear_job_deleted_marker(job["job_id"])































    job["error_message"] = ""



































    job["completion_notified_at"] = ""































    job["completion_notification_error"] = ""



























    job["progress_detail"] = "从断点继续执行任务"
    if reconcile_job_outputs_from_public_artifacts(job, persist=True, notify=True):
        return
    if selected_job_outputs_ready(job):
        job["status"] = "done"
        job["progress"] = 100
        job["progress_detail"] = "全部产物已生成"
        upsert_job_record(job)
        notify_job_creator_on_completion(job)
        return































    if job.get("output_video_url") and not job.get("output_video_no_bgm_url"):































        job["status"] = "rendering"































        job["progress"] = max(82, clamp_progress(job.get("progress", 0)))































    elif job.get("cover_16x9_url"):































        job["status"] = "processing_cover"































        job["progress"] = max(44, clamp_progress(job.get("progress", 0)))































    else:































        job["status"] = "queued"































        job["progress"] = max(2, clamp_progress(job.get("progress", 0)))































    upsert_job_record(job)































    run_job_async(job)































































































def retry_job(job_id):
    lock = get_named_runtime_lock(JOB_RETRY_LOCKS, JOB_RETRY_LOCKS_LOCK, job_id)
    if not lock.acquire(blocking=False):
        raise ValueError("任务正在重新制作，请勿重复提交")
    try:































        job = fetch_job_row(job_id)































        if not job:































            raise ValueError("任务不存在")































        if job.get("status") == "done":































            raise ValueError("任务已完成，无需重新制作")































        if job.get("status") != "failed":
            raise ValueError("任务正在处理中，无需重复提交")
        resume_job_from_checkpoint(job)
    finally:
        lock.release()































    return {"job_id": job["job_id"], "resumed": True}































































































def parse_job_route(path):































    match = re.match(r"^/api/drama-material/jobs/([0-9a-f]{32})(?:/(retry))?$", path)































    if not match:































        return None, None































    return match.group(1), match.group(2)































































































class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):































    daemon_threads = True































































































class DramaMaterialHandler(BaseHTTPRequestHandler):































    server_version = "DramaMaterialAPI/2.0"































































    def log_message(self, fmt, *args):































        logging.info("%s - %s", self.address_string(), fmt % args)































































    def _read_json(self):































        content_length = int(self.headers.get("Content-Length", "0"))































        if content_length <= 0:































            return {}































        body = self.rfile.read(content_length)































        return json.loads(body.decode("utf-8")) if body else {}































































    def _cookies(self):































        return parse_cookie_header(self.headers.get("Cookie", ""))































































    def _session(self):































        token_session = self._api_token_session()
        if token_session:
            return token_session
        return load_session(self._cookies().get(SESSION_COOKIE_NAME, ""))

    def _request_api_token(self):
        auth = self.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1].strip()
        return self.headers.get("X-API-Token", "").strip()

    def _api_token_session(self):
        token = self._request_api_token()
        if not token or not SCREENSHOT_API_TOKENS:
            return None
        for expected in SCREENSHOT_API_TOKENS:
            if secrets.compare_digest(token, expected):
                permissions = dict(DEFAULT_USER_PERMISSIONS)
                permissions["cover_synthesis"] = True
                return {
                    "session_token": "",
                    "user_id": "api:%s" % SCREENSHOT_API_TOKEN_NAME,
                    "union_id": "",
                    "open_id": "",
                    "name": SCREENSHOT_API_TOKEN_NAME,
                    "en_name": "",
                    "avatar_url": "",
                    "tenant_key": "api",
                    "source": "api_token",
                    "role": "user",
                    "permissions": permissions,
                    "auth_type": "api_token",
                }
        return None































































    def _auth_payload(self):































        session = self._session()































        return {































            "enabled": feishu_auth_enabled(),































            "authenticated": bool(session) or not feishu_auth_enabled(),































            "login_url": "/api/auth/feishu/login" if feishu_auth_enabled() else "",































            "feishu_app_id": FEISHU_APP_ID if feishu_auth_enabled() else "",































            "allowed_tenant_keys": FEISHU_ALLOWED_TENANT_KEYS,































            "modules": MODULE_PERMISSIONS,































            "user": {































                "user_id": session.get("user_id", ""),































                "name": session.get("name", ""),































                "avatar_url": session.get("avatar_url", ""),































                "source": session.get("source", ""),































                "role": session.get("role", "user"),































                "tenant_key": session.get("tenant_key", ""),































                "email": session.get("email", ""),

                "is_admin": session.get("role", "user") == "admin",































                "permissions": normalize_user_permissions(session.get("permissions", {}), session.get("role", "user")),































            } if session else None,































        }































































    def _auth_required_payload(self):
        auth = self._auth_payload()
        return {
            "code": "auth_required",
            "error": "auth_required",
            "message": "未提供 Cookie 或有效 token。",
            "authenticated": False,
            "login_url": auth.get("login_url", ""),
            "token_supported": bool(SCREENSHOT_API_TOKENS),
            "auth": auth,
        }

    def _send_report_auth_status(self):
        if not feishu_auth_enabled() or self._session():
            self.send_response(204)
        else:
            self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _require_auth(self):































        if not feishu_auth_enabled():































            return True































        if self._session():































            return True































        json_response(self, 401, self._auth_required_payload())































        return False































































    def _require_admin(self):































        if not self._require_auth():































            return False































        session = self._session()































        if session and session.get("role") == "admin":































            return True































        json_response(self, 403, {"error": "admin_required"})































        return False































































    def _require_module(self, module_key):































        if not self._require_auth():































            return False































        session = self._session()































        if has_module_permission(session, module_key):































            return True































        json_response(self, 403, {"error": "permission_denied", "module": module_key})































        return False


    def _require_cookie_module(self, module_key):
        if not self._require_auth():
            return False
        session = self._session()
        if session and session.get("auth_type") == "api_token":
            json_response(self, 403, {"error": "cookie_auth_required", "module": module_key})
            return False
        if has_module_permission(session, module_key):
            return True
        json_response(self, 403, {"error": "permission_denied", "module": module_key})
        return False

    def _require_any_module(self, module_keys):
        if not self._require_auth():
            return False
        session = self._session()
        for module_key in module_keys:
            if has_module_permission(session, module_key):
                return True
        json_response(self, 403, {"error": "permission_denied", "modules": list(module_keys)})
        return False















































    def _finish_login(self, user_info, source, redirect_to):































        session = create_user_session(user_info, source)































        self.send_response(302)































        set_cookie_header(self, SESSION_COOKIE_NAME, session["session_token"], max_age=SESSION_TTL_SECONDS)































        self.send_header("Location", redirect_to if redirect_to.startswith("/") else "/")































        self.send_header("Content-Length", "0")































        self.end_headers()































































    def do_GET(self):































        parsed = urlparse(self.path)































        if parsed.path == "/api/auth/status":































            json_response(self, 200, self._auth_payload())































            return

        if parsed.path == "/api/report-auth/tt-minis-native-growth":
            self._send_report_auth_status()
            return

        if parsed.path == "/api/ui/topbar":
            json_response(self, 200, self._auth_payload())
            return































        if parsed.path == "/api/admin/users":































            if not self._require_admin():































                return































            json_response(self, 200, {"items": list_admin_users()})































            return































        if parsed.path == "/api/admin/logs":































            if not self._require_admin():































                return































            params = parse_qs(parsed.query)































            limit = int((params.get("limit") or ["200"])[0])































            json_response(self, 200, list_audit_logs(limit=limit))































            return































        if parsed.path == "/api/admin/navigation":
            if not self._require_admin():
                return
            try:
                json_response(self, 200, {"items": load_navigation_config()})
            except Exception as exc:
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/voiceover-drama/designers":
            if not self._require_module("voiceover_drama_tasks"):
                return
            try:
                json_response(self, 200, list_voiceover_designers())
            except Exception as exc:
                code = 403 if isinstance(exc, PermissionError) else 400
                json_response(self, code, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/products":
            if not self._require_module("ad_control_center"):
                return
            try:
                params = parse_qs(parsed.query)
                json_response(
                    self,
                    200,
                    list_ad_control_products(
                        query=(params.get("q") or [""])[0],
                        limit=(params.get("limit") or ["200"])[0],
                    ),
                )
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/accounts":
            if not self._require_module("ad_control_center"):
                return
            try:
                params = parse_qs(parsed.query)
                json_response(self, 200, list_ad_control_accounts((params.get("product") or [""])[0]))
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/rules":
            if not self._require_module("ad_control_center"):
                return
            try:
                json_response(self, 200, list_ad_control_rules())
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/token-config":
            if not self._require_module("ad_control_center"):
                return
            try:
                params = parse_qs(parsed.query)
                json_response(self, 200, list_ad_control_token_config((params.get("product") or [""])[0]))
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/account-groups":
            if not self._require_module("ad_control_center"):
                return
            try:
                params = parse_qs(parsed.query)
                json_response(self, 200, list_ad_control_account_groups((params.get("product") or [""])[0]))
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/rule-sets":
            if not self._require_module("ad_control_center"):
                return
            try:
                params = parse_qs(parsed.query)
                json_response(self, 200, list_ad_control_rule_sets((params.get("product") or [""])[0]))
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        rule_set_id = ad_control_parse_rule_set_path(parsed.path)
        if rule_set_id:
            if not self._require_module("ad_control_center"):
                return
            try:
                json_response(self, 200, fetch_ad_control_rule_set(rule_set_id))
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/bindings":
            if not self._require_module("ad_control_center"):
                return
            try:
                params = parse_qs(parsed.query)
                json_response(self, 200, list_ad_control_bindings((params.get("product") or [""])[0]))
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        binding_id = ad_control_parse_binding_path(parsed.path)
        if binding_id:
            if not self._require_module("ad_control_center"):
                return
            try:
                json_response(self, 200, fetch_ad_control_binding(binding_id))
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/rule-groups":
            if not self._require_module("ad_control_center"):
                return
            try:
                params = parse_qs(parsed.query)
                json_response(self, 200, list_ad_control_rule_groups((params.get("product") or [""])[0]))
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/runner/status":
            if not self._require_module("ad_control_center"):
                return
            try:
                json_response(self, 200, ad_control_runner_status())
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/actions":
            if not self._require_module("ad_control_center"):
                return
            try:
                params = parse_qs(parsed.query)
                json_response(
                    self,
                    200,
                    list_ad_control_actions(
                        limit=(params.get("limit") or ["50"])[0],
                        product=(params.get("product") or [""])[0],
                        binding_id=(params.get("binding_id") or [""])[0],
                        action=(params.get("action") or [""])[0],
                        date_from=(params.get("date_from") or [""])[0],
                        date_to=(params.get("date_to") or [""])[0],
                    ),
                )
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-material/competitor-sources":
            if not self._require_module("ad_material_tasks"):
                return
            try:
                include_disabled = (parse_qs(parsed.query).get("include_disabled") or [""])[0] in ("1", "true", "yes")
                json_response(self, 200, {"items": list_ad_material_competitor_sources(include_disabled=include_disabled)})
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-material/products":
            if not self._require_module("ad_material_tasks"):
                return
            try:
                product_params = parse_qs(parsed.query)
                product_query = (product_params.get("q") or [""])[0]
                product_limit = (product_params.get("limit") or ["80"])[0]
                json_response(
                    self,
                    200,
                    list_ad_material_products(
                        self._session(),
                        query=product_query,
                        limit=product_limit,
                        with_total=True,
                    ),
                )
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-material/tasks":
            if not self._require_module("ad_material_tasks"):
                return
            try:
                json_response(self, 200, list_ad_material_tasks(self._session(), parse_qs(parsed.query)))
            except Exception as exc:
                code = 403 if isinstance(exc, PermissionError) else 400
                json_response(self, code, api_error_payload(exc))
            return

        ad_task_id, ad_action = parse_ad_material_task_route(parsed.path)
        if ad_task_id and not ad_action:
            if not self._require_module("ad_material_tasks"):
                return
            try:
                task = fetch_ad_material_task(ad_task_id)
                ensure_ad_material_access(self._session(), task)
                json_response(self, 200, task)
            except Exception as exc:
                code = 403 if isinstance(exc, PermissionError) else 400
                json_response(self, code, api_error_payload(exc))
            return

        if parsed.path == "/api/auth/feishu/login":































            if not feishu_auth_enabled():































                json_response(self, 400, {"error": "feishu_auth_not_configured"})































                return































            params = parse_qs(parsed.query)































            next_path = (params.get("next") or ["/"])[0]































            state = create_feishu_login_state(next_path=next_path)































            redirect_response(self, build_feishu_login_url(state))































            return































        if parsed.path == "/api/auth/feishu/callback":































            if not feishu_auth_enabled():































                json_response(self, 400, {"error": "feishu_auth_not_configured"})































                return































            params = parse_qs(parsed.query)































            code = (params.get("code") or [""])[0].strip()































            state_token = (params.get("state") or [""])[0].strip()































            state_item = pop_feishu_login_state(state_token)































            if not code or not state_item:































                redirect_response(self, "/?auth_error=invalid_feishu_callback")































                return































            try:































                user_info = exchange_feishu_code_for_user(code)































                self._finish_login(user_info, "feishu_web", state_item.get("next_path") or "/")































            except Exception as exc:































                logging.exception("feishu callback failed")































                redirect_response(self, "/?auth_error=" + quote(str(exc)))































            return































        if parsed.path == "/api/drama-material/products":































            if not self._require_any_module(("drama_synthesis", "cover_synthesis")):































                return































            try:































                json_response(self, 200, {"items": list_products()})































            except Exception as exc:































                json_response(self, 500, {"error": str(exc)})































            return































        screenshot_job_id, screenshot_action = parse_screenshot_job_route(parsed.path)

        if screenshot_job_id and not screenshot_action:

            if not self._require_module("cover_synthesis"):

                return

            try:

                payload = fetch_screenshot_job_row(screenshot_job_id)

                if not payload:

                    json_response(self, 404, {"error": "not_found"})

                    return

                json_response(self, 200, payload)

            except Exception as exc:

                json_response(self, 500, {"error": str(exc)})

            return

        if parsed.path == "/api/drama-screenshot-material/jobs":

            if not self._require_module("cover_synthesis"):

                return

            try:

                params = parse_qs(parsed.query)

                payload = fetch_screenshot_job_rows(

                    job_id=(params.get("job_id") or [""])[0].strip() or None,

                    app_id=(params.get("app_id") or [""])[0].strip() or None,

                    content_id=(params.get("content_id") or [""])[0].strip() or None,

                    status=(params.get("status") or [""])[0].strip() or None,

                    query=(params.get("q") or [""])[0].strip() or None,

                    date_from=(params.get("date_from") or [""])[0].strip() or None,

                    date_to=(params.get("date_to") or [""])[0].strip() or None,

                    page=int((params.get("page") or ["1"])[0]),

                    page_size=int((params.get("page_size") or ["20"])[0]),

                )

                json_response(self, 200, payload)

            except Exception as exc:

                json_response(self, 400, api_error_payload(exc))

            return

        if parsed.path == "/api/drama-screenshot-material/jobs/batch":

            if not self._require_module("cover_synthesis"):

                return

            try:

                payload = submit_screenshot_job_batch(self._read_json(), self._session())

                append_audit_log(

                    self._session(),

                    "create_screenshot_job_batch",

                    "screenshot_job",

                    "",

                    {

                        "app_id": payload.get("app_id", ""),

                        "count": payload.get("count", 0),

                        "accepted_count": payload.get("accepted_count", 0),

                        "duplicate_count": payload.get("duplicate_count", 0),

                        "failed_count": payload.get("failed_count", 0),

                    },

                )

                json_response(self, 202, payload)

            except Exception as exc:

                json_response(self, 400, api_error_payload(exc))

            return

        if parsed.path == "/api/drama-screenshot-material/jobs":

            if not self._require_module("cover_synthesis"):

                return

            try:

                payload = submit_screenshot_job(self._read_json(), self._session())

                append_audit_log(

                    self._session(),

                    "create_screenshot_job",

                    "screenshot_job",

                    payload.get("job_id", ""),

                    payload,

                )

                json_response(self, 202, payload)

            except Exception as exc:

                json_response(self, 400, api_error_payload(exc))

            return

        if parsed.path == "/api/drama-screenshot-material/jobs":

            if not self._require_module("cover_synthesis"):

                return

            try:

                payload = submit_screenshot_job(self._read_json(), self._session())

                append_audit_log(

                    self._session(),

                    "create_screenshot_job",

                    "screenshot_job",

                    payload.get("job_id", ""),

                    payload,

                )

                json_response(self, 202, payload)

            except Exception as exc:

                json_response(self, 400, api_error_payload(exc))

            return

        if parsed.path == "/api/drama-material/jobs":





























            if not self._require_module("drama_synthesis"):































                return































            try:































                params = parse_qs(parsed.query)































                payload = fetch_job_rows(































                    job_id=(params.get("job_id") or [""])[0].strip() or None,































                    app_id=(params.get("app_id") or [""])[0].strip() or None,































                    content_id=(params.get("content_id") or [""])[0].strip() or None,































                    status=(params.get("status") or [""])[0].strip() or None,































                    query=(params.get("q") or [""])[0].strip() or None,































                    date_from=(params.get("date_from") or [""])[0].strip() or None,































                    date_to=(params.get("date_to") or [""])[0].strip() or None,































                    page=int((params.get("page") or ["1"])[0]),































                    page_size=int((params.get("page_size") or ["20"])[0]),































                )































                json_response(self, 200, payload)































            except Exception as exc:































                json_response(self, 400, api_error_payload(exc))































            return































        job_id, action = parse_job_route(parsed.path)































        if job_id and not action:































            if not self._require_module("drama_synthesis"):































                return































            job = fetch_job_row(job_id)































            if not job:































                json_response(self, 404, {"error": "not_found"})































                return































            json_response(self, 200, job)































            return































        json_response(self, 404, {"error": "not_found"})































































    def do_POST(self):

        parsed = urlparse(self.path)

        if parsed.path == "/api/gpu-video/render":
            try:
                auth = self.headers.get("Authorization", "")
                token = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
                if not GPU_VIDEO_WORKER_TOKEN or not secrets.compare_digest(token, GPU_VIDEO_WORKER_TOKEN):
                    json_response(self, 403, {"error": "forbidden"})
                    return
                json_response(self, 200, handle_gpu_video_render(self._read_json()))
            except Exception as exc:
                logging.exception("gpu video render failed")
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/gpu-video/cover":
            try:
                auth = self.headers.get("Authorization", "")
                token = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
                if not GPU_VIDEO_WORKER_TOKEN or not secrets.compare_digest(token, GPU_VIDEO_WORKER_TOKEN):
                    json_response(self, 403, {"error": "forbidden"})
                    return
                json_response(self, 200, handle_gpu_video_cover(self._read_json()))
            except Exception as exc:
                logging.exception("gpu video cover failed")
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/auth/logout":

            session_token = self._cookies().get(SESSION_COOKIE_NAME, "")

            delete_session(session_token)

            self.send_response(200)

            set_cookie_header(self, SESSION_COOKIE_NAME, "", max_age=0)

            body = json.dumps({"message": "logged_out"}, ensure_ascii=False).encode("utf-8")

            self.send_header("Content-Type", "application/json; charset=utf-8")

            self.send_header("Content-Length", str(len(body)))

            self.end_headers()

            self.wfile.write(body)

            return

        if parsed.path == "/api/auth/feishu/exchange":

            if not feishu_auth_enabled():

                json_response(self, 400, {"error": "feishu_auth_not_configured"})

                return

            try:

                payload = self._read_json()

                code = str(payload.get("code", "") or "").strip()

                next_path = str(payload.get("next", "/") or "/")

                if not code:

                    raise ValueError("missing feishu auth code")

                user_info = exchange_feishu_code_for_user(code)

                session = create_user_session(user_info, "feishu_internal")

                self.send_response(200)

                set_cookie_header(self, SESSION_COOKIE_NAME, session["session_token"], max_age=SESSION_TTL_SECONDS)

                body = json.dumps(

                    {

                        "message": "ok",

                        "next": next_path if next_path.startswith("/") else "/",

                        "user": self._auth_payload()["user"],

                    },

                    ensure_ascii=False,

                ).encode("utf-8")

                self.send_header("Content-Type", "application/json; charset=utf-8")

                self.send_header("Content-Length", str(len(body)))

                self.end_headers()

                self.wfile.write(body)

            except Exception as exc:

                code = 403 if isinstance(exc, PermissionError) else 400

                json_response(self, code, {"error": str(exc)})

            return

        if parsed.path == "/api/admin/navigation":
            if not self._require_admin():
                return
            try:
                payload = self._read_json()
                config = payload.get("items", payload)
                saved = save_navigation_config(config)
                append_audit_log(self._session(), "update_navigation", "navigation", "quick_nav", {"items": saved})
                json_response(self, 200, {"items": saved})
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/admin/users/role":

            if not self._require_admin():

                return

            try:

                payload = self._read_json()

                result = update_admin_user_role(

                    str(payload.get("user_id", "") or "").strip(),

                    payload.get("role", "user"),

                    self._session(),

                )

                json_response(self, 200, result)

            except Exception as exc:

                json_response(self, 400, api_error_payload(exc))

            return

        if parsed.path == "/api/admin/users/permissions":

            if not self._require_admin():

                return

            try:

                payload = self._read_json()

                result = update_admin_user_permissions(

                    str(payload.get("user_id", "") or "").strip(),

                    payload.get("permissions", {}),

                    self._session(),

                )

                json_response(self, 200, result)

            except Exception as exc:

                json_response(self, 400, api_error_payload(exc))

            return

        if parsed.path == "/api/ad-control/preview":
            if not self._require_module("ad_control_center"):
                return
            try:
                payload = create_ad_control_preview(self._read_json(), self._session())
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/execute":
            if not self._require_module("ad_control_center"):
                return
            try:
                payload = execute_ad_control(self._read_json(), self._session())
                append_audit_log(
                    self._session(),
                    "execute_ad_control",
                    "ad_control",
                    payload.get("action_id", ""),
                    {
                        "preview_id": payload.get("preview_id", ""),
                        "action": payload.get("action", ""),
                        "requested_count": payload.get("requested_count", 0),
                        "success_count": payload.get("success_count", 0),
                        "skipped_count": payload.get("skipped_count", 0),
                        "error_count": payload.get("error_count", 0),
                        "dry_run": payload.get("dry_run", False),
                    },
                )
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/rules":
            if not self._require_module("ad_control_center"):
                return
            try:
                payload = save_ad_control_rule(self._read_json(), self._session())
                append_audit_log(self._session(), "save_ad_control_rule", "ad_control_rule", payload.get("rule_id", ""), payload)
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path.startswith("/api/ad-control/rules/") and parsed.path.endswith("/enabled"):
            if not self._require_module("ad_control_center"):
                return
            try:
                rule_id = parsed.path[len("/api/ad-control/rules/"):-len("/enabled")].strip("/")
                body = self._read_json()
                payload = set_ad_control_rule_enabled(rule_id, bool(body.get("enabled")))
                append_audit_log(
                    self._session(),
                    "set_ad_control_rule_enabled",
                    "ad_control_rule",
                    payload.get("rule_id", ""),
                    {"enabled": payload.get("enabled", False)},
                )
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/token-config":
            if not self._require_module("ad_control_center"):
                return
            try:
                payload = save_ad_control_token_config(self._read_json(), self._session())
                append_audit_log(self._session(), "save_ad_control_token_config", "ad_control_token_config", payload.get("product", ""), payload)
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/token-config/validate":
            if not self._require_module("ad_control_center"):
                return
            try:
                json_response(self, 200, validate_ad_control_token_config(self._read_json()))
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/account-groups":
            if not self._require_module("ad_control_center"):
                return
            try:
                payload = save_ad_control_account_group(self._read_json(), self._session())
                append_audit_log(self._session(), "save_ad_control_account_group", "ad_control_account_group", payload.get("group_id", ""), payload)
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/rule-sets":
            if not self._require_module("ad_control_center"):
                return
            try:
                payload = save_ad_control_rule_set(self._read_json(), self._session())
                append_audit_log(self._session(), "save_ad_control_rule_set", "ad_control_rule_set", payload.get("rule_set_id", ""), payload)
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        rule_set_id = ad_control_parse_rule_set_path(parsed.path)
        if rule_set_id:
            if not self._require_module("ad_control_center"):
                return
            try:
                body = self._read_json()
                body["rule_set_id"] = rule_set_id
                payload = save_ad_control_rule_set(body, self._session())
                append_audit_log(self._session(), "save_ad_control_rule_set", "ad_control_rule_set", payload.get("rule_set_id", ""), payload)
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/bindings":
            if not self._require_module("ad_control_center"):
                return
            try:
                payload = save_ad_control_binding(self._read_json(), self._session())
                append_audit_log(self._session(), "save_ad_control_binding", "ad_control_binding", payload.get("binding_id", ""), payload)
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path.startswith("/api/ad-control/bindings/") and parsed.path.endswith("/enabled"):
            if not self._require_module("ad_control_center"):
                return
            try:
                binding_id = ad_control_parse_binding_path(parsed.path, "/enabled")
                body = self._read_json()
                payload = set_ad_control_binding_enabled(binding_id, bool(body.get("enabled")))
                append_audit_log(self._session(), "set_ad_control_binding_enabled", "ad_control_binding", payload.get("binding_id", ""), {"enabled": payload.get("enabled", False)})
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path.startswith("/api/ad-control/bindings/") and parsed.path.endswith("/preview-live"):
            if not self._require_module("ad_control_center"):
                return
            try:
                body = self._read_json()
                body["rule_group_id"] = ad_control_parse_binding_path(parsed.path, "/preview-live")
                payload = create_ad_control_live_preview(body, self._session())
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path.startswith("/api/ad-control/bindings/") and parsed.path.endswith("/execute-live"):
            if not self._require_module("ad_control_center"):
                return
            try:
                body = self._read_json()
                body["rule_group_id"] = ad_control_parse_binding_path(parsed.path, "/execute-live")
                payload = execute_ad_control_live(body, self._session())
                append_audit_log(self._session(), "execute_ad_control_live", "ad_control_binding", body.get("rule_group_id", ""), payload)
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        binding_id = ad_control_parse_binding_path(parsed.path)
        if binding_id:
            if not self._require_module("ad_control_center"):
                return
            try:
                body = self._read_json()
                body["group_id"] = binding_id
                payload = save_ad_control_binding(body, self._session())
                append_audit_log(self._session(), "save_ad_control_binding", "ad_control_binding", payload.get("binding_id", ""), payload)
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/rule-groups":
            if not self._require_module("ad_control_center"):
                return
            try:
                payload = save_ad_control_rule_group(self._read_json(), self._session())
                append_audit_log(self._session(), "save_ad_control_rule_group", "ad_control_rule_group", payload.get("group_id", ""), payload)
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path.startswith("/api/ad-control/rule-groups/") and parsed.path.endswith("/enabled"):
            if not self._require_module("ad_control_center"):
                return
            try:
                rule_group_id = ad_control_parse_rule_group_path(parsed.path, "/enabled")
                body = self._read_json()
                payload = set_ad_control_rule_group_enabled(rule_group_id, bool(body.get("enabled")))
                append_audit_log(self._session(), "set_ad_control_rule_group_enabled", "ad_control_rule_group", payload.get("group_id", ""), {"enabled": payload.get("enabled", False)})
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path.startswith("/api/ad-control/rule-groups/") and parsed.path.endswith("/preview-live"):
            if not self._require_module("ad_control_center"):
                return
            try:
                body = self._read_json()
                body["rule_group_id"] = ad_control_parse_rule_group_path(parsed.path, "/preview-live")
                payload = create_ad_control_live_preview(body, self._session())
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path.startswith("/api/ad-control/rule-groups/") and parsed.path.endswith("/execute-live"):
            if not self._require_module("ad_control_center"):
                return
            try:
                body = self._read_json()
                body["rule_group_id"] = ad_control_parse_rule_group_path(parsed.path, "/execute-live")
                payload = execute_ad_control_live(body, self._session())
                append_audit_log(self._session(), "execute_ad_control_live", "ad_control_rule_group", body.get("rule_group_id", ""), payload)
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/campaign-start/refresh":
            if not self._require_module("ad_control_center"):
                return
            try:
                json_response(self, 200, refresh_ad_control_campaign_start(self._read_json()))
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-control/emergency-stop":
            if not self._require_module("ad_control_center"):
                return
            try:
                payload = ad_control_emergency_stop(self._read_json())
                append_audit_log(self._session(), "ad_control_emergency_stop", "ad_control", payload.get("group_id", ""), payload)
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        screenshot_job_id, screenshot_action = parse_screenshot_job_route(parsed.path)
        if screenshot_job_id and screenshot_action == "retry":
            if not self._require_module("cover_synthesis"):
                return
            try:
                payload = retry_screenshot_job(screenshot_job_id)
                append_audit_log(self._session(), "retry_screenshot_job", "screenshot_job", screenshot_job_id, payload)
                json_response(self, 202, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/voiceover-drama/material-counts":
            if not self._require_module("voiceover_drama_tasks"):
                return
            try:
                payload = voiceover_material_counts(self._read_json())
                append_audit_log(self._session(), "voiceover_material_counts", "voiceover_drama", "", {"total": payload.get("total", 0)})
                json_response(self, 200, payload)
            except Exception as exc:
                code = 403 if isinstance(exc, PermissionError) else 400
                json_response(self, code, api_error_payload(exc))
            return

        if parsed.path == "/api/voiceover-drama/filter":
            if not self._require_module("voiceover_drama_tasks"):
                return
            try:
                payload = voiceover_filter_materials(self._read_json())
                append_audit_log(
                    self._session(),
                    "voiceover_filter_materials",
                    "voiceover_drama",
                    "",
                    {
                        "total": payload.get("total", 0),
                        "groups": len(payload.get("groups") or []),
                    },
                )
                json_response(self, 200, payload)
            except Exception as exc:
                code = 403 if isinstance(exc, PermissionError) else 400
                json_response(self, code, api_error_payload(exc))
            return

        if parsed.path == "/api/voiceover-drama/design-tasks":
            if not self._require_module("voiceover_drama_tasks"):
                return
            try:
                payload = create_voiceover_design_tasks(self._read_json(), self._session())
                append_audit_log(
                    self._session(),
                    "create_voiceover_design_tasks",
                    "voiceover_drama",
                    "",
                    {
                        "created_count": payload.get("created_count", 0),
                        "failed_count": payload.get("failed_count", 0),
                    },
                )
                json_response(self, 200 if not payload.get("failed_count") else 207, payload)
            except Exception as exc:
                code = 403 if isinstance(exc, PermissionError) else 400
                json_response(self, code, api_error_payload(exc))
            return

        if parsed.path == "/api/ad-material/tasks":
            if not self._require_module("ad_material_tasks"):
                return
            try:
                payload = create_ad_material_task(self._read_json(), self._session())
                append_audit_log(self._session(), "create_ad_material_task", "ad_material_task", payload.get("task_id", ""), payload)
                json_response(self, 201, payload)
            except Exception as exc:
                code = 403 if isinstance(exc, PermissionError) else 400
                json_response(self, code, api_error_payload(exc))
            return

        ad_task_id, ad_action = parse_ad_material_task_route(parsed.path)
        if ad_task_id:
            if not self._require_module("ad_material_tasks"):
                return
            try:
                body = self._read_json()
                if ad_action == "":
                    payload = update_ad_material_task(ad_task_id, body, self._session())
                    audit_action = "update_ad_material_task"
                elif ad_action == "copy":
                    payload = copy_ad_material_task(ad_task_id, self._session())
                    audit_action = "copy_ad_material_task"
                elif ad_action == "publish":
                    payload = publish_ad_material_task(ad_task_id, self._session())
                    audit_action = "publish_ad_material_task"
                elif ad_action == "demand-review":
                    payload = review_ad_material_demand(ad_task_id, body, self._session())
                    audit_action = "review_ad_material_demand"
                elif ad_action == "export-pdf":
                    payload = export_ad_material_demand_pdf(ad_task_id, self._session())
                    audit_action = "export_ad_material_demand_pdf"
                elif ad_action == "complete-upload":
                    payload = complete_ad_material_upload(ad_task_id, self._session())
                    audit_action = "complete_ad_material_upload"
                elif ad_action.startswith("asset-review:"):
                    payload = review_ad_material_asset(ad_task_id, ad_action.split(":", 1)[1], body, self._session())
                    audit_action = "review_ad_material_asset"
                else:
                    json_response(self, 404, {"error": "not_found"})
                    return
                append_audit_log(self._session(), audit_action, "ad_material_task", ad_task_id, {"status": payload.get("status", "")})
                json_response(self, 202 if audit_action.startswith(("publish", "review")) else 200, payload)
            except Exception as exc:
                code = 403 if isinstance(exc, PermissionError) else 400
                json_response(self, code, api_error_payload(exc))
            return

        if parsed.path == "/api/drama-screenshot-material/jobs/delete-batch":
            if not self._require_cookie_module("cover_synthesis"):
                return
            try:
                payload = self._read_json()
                result = delete_screenshot_jobs(payload.get("job_ids", []))
                append_audit_log(
                    self._session(),
                    "delete_screenshot_job_batch",
                    "screenshot_job",
                    "",
                    {
                        "requested_count": result.get("requested_count", 0),
                        "deleted_count": result.get("deleted_count", 0),
                        "missing_count": result.get("missing_count", 0),
                    },
                )
                json_response(self, 200, result)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        if parsed.path == "/api/drama-screenshot-material/jobs/batch":

            if not self._require_module("cover_synthesis"):

                return

            try:

                payload = submit_screenshot_job_batch(self._read_json(), self._session())

                append_audit_log(

                    self._session(),

                    "create_screenshot_job_batch",

                    "screenshot_job",

                    "",

                    {

                        "app_id": payload.get("app_id", ""),

                        "count": payload.get("count", 0),

                        "accepted_count": payload.get("accepted_count", 0),

                        "duplicate_count": payload.get("duplicate_count", 0),

                        "failed_count": payload.get("failed_count", 0),

                    },

                )

                json_response(self, 202, payload)

            except Exception as exc:

                json_response(self, 400, api_error_payload(exc))

            return

        if parsed.path == "/api/drama-screenshot-material/jobs":

            if not self._require_module("cover_synthesis"):

                return

            try:

                payload = submit_screenshot_job(self._read_json(), self._session())

                append_audit_log(

                    self._session(),

                    "create_screenshot_job",

                    "screenshot_job",

                    payload.get("job_id", ""),

                    payload,

                )

                json_response(self, 202, payload)

            except Exception as exc:

                json_response(self, 400, api_error_payload(exc))

            return

        if parsed.path == "/api/drama-material/jobs":

            if not self._require_module("drama_synthesis"):

                return

            try:

                payload = submit_job(self._read_json(), self._session())

                append_audit_log(self._session(), "create_job", "job", payload.get("job_id", ""), payload)

                json_response(self, 202, payload)

            except Exception as exc:

                json_response(self, 400, api_error_payload(exc))

            return

        job_id, action = parse_job_route(parsed.path)

        if job_id and action == "retry":

            if not self._require_module("drama_synthesis"):

                return

            try:

                payload = retry_job(job_id)

                append_audit_log(self._session(), "retry_job", "job", job_id, payload)

                json_response(self, 202, payload)

            except Exception as exc:

                json_response(self, 400, api_error_payload(exc))

            return

        json_response(self, 404, {"error": "not_found"})



    def do_PUT(self):
        self.do_POST()


    def do_DELETE(self):































        parsed = urlparse(self.path)

        account_group_id = ad_control_parse_account_group_path(parsed.path)
        if account_group_id:
            if not self._require_module("ad_control_center"):
                return
            try:
                payload = delete_ad_control_account_group(account_group_id)
                append_audit_log(self._session(), "delete_ad_control_account_group", "ad_control_account_group", account_group_id, {})
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        rule_set_id = ad_control_parse_rule_set_path(parsed.path)
        if rule_set_id:
            if not self._require_module("ad_control_center"):
                return
            try:
                payload = delete_ad_control_rule_set(rule_set_id)
                append_audit_log(self._session(), "delete_ad_control_rule_set", "ad_control_rule_set", rule_set_id, {})
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        binding_id = ad_control_parse_binding_path(parsed.path)
        if binding_id:
            if not self._require_module("ad_control_center"):
                return
            try:
                payload = delete_ad_control_binding(binding_id)
                append_audit_log(self._session(), "delete_ad_control_binding", "ad_control_binding", binding_id, {})
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        rule_group_id = ad_control_parse_rule_group_path(parsed.path)
        if rule_group_id:
            if not self._require_module("ad_control_center"):
                return
            try:
                payload = delete_ad_control_rule_group(rule_group_id)
                append_audit_log(self._session(), "delete_ad_control_rule_group", "ad_control_rule_group", rule_group_id, {})
                json_response(self, 200, payload)
            except Exception as exc:
                json_response(self, 400, api_error_payload(exc))
            return

        ad_task_id, ad_action = parse_ad_material_task_route(parsed.path)
        if ad_task_id and not ad_action:
            if not self._require_module("ad_material_tasks"):
                return
            try:
                result = delete_ad_material_task(ad_task_id, self._session())
                append_audit_log(self._session(), "delete_ad_material_task", "ad_material_task", ad_task_id, {})
                json_response(self, 200, result)
            except Exception as exc:
                code = 403 if isinstance(exc, PermissionError) else 400
                json_response(self, code, api_error_payload(exc))
            return

        screenshot_job_id, screenshot_action = parse_screenshot_job_route(parsed.path)
        if screenshot_job_id and not screenshot_action:
            if not self._require_cookie_module("cover_synthesis"):
                return
            result = delete_screenshot_job(screenshot_job_id)
            if result:
                append_audit_log(self._session(), "delete_screenshot_job", "screenshot_job", screenshot_job_id, {})
                json_response(self, 200, {"message": "deleted", "job_id": screenshot_job_id})
            else:
                json_response(self, 404, {"error": "not_found"})
            return































        job_id, action = parse_job_route(parsed.path)































        if job_id and not action:































            if not self._require_module("drama_synthesis"):































                return































            if delete_job(job_id):































                append_audit_log(self._session(), "delete_job", "job", job_id, {})































                json_response(self, 200, {"message": "deleted", "job_id": job_id})































            else:































                json_response(self, 404, {"error": "not_found"})































            return































        json_response(self, 404, {"error": "not_found"})































































































def main():





























    ensure_dir(WORK_ROOT)





























    ensure_dir(PUBLIC_ROOT)

    ensure_dir(SCREENSHOT_WORK_ROOT)

    ensure_dir(SCREENSHOT_PUBLIC_ROOT)

    ensure_dir(AD_MATERIAL_WORK_ROOT)

    ensure_dir(AD_MATERIAL_PUBLIC_ROOT)





























    ensure_job_table()

    ensure_screenshot_job_table()

    ensure_ad_material_tables()

    ensure_ad_control_tables()





























    ensure_auth_session_table()





























    ensure_user_table()































    ensure_audit_log_table()















    backfill_users_from_sessions()















    backfill_audit_logs()















    backfill_job_creators_from_audit_logs()















    cleanup_expired_sessions()















    recover_inflight_jobs()
    recover_inflight_screenshot_jobs()
    recover_inflight_ad_material_tasks()






























    server = ThreadedHTTPServer((HOST, PORT), DramaMaterialHandler)































    logging.info("listening on %s:%s", HOST, PORT)































    server.serve_forever()

if __name__ == "__main__":
    main()
