"""Bind a managed X identity to the Ads API using a separate OAuth 1.0a grant."""
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode

X_API = "https://api.x.com"
ADS_API = "https://ads-api.x.com/12"
AUTHORIZE = X_API + "/oauth/authorize"
HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")


class AdsError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def signed(config, token="", secret="", **kwargs):
    from requests_oauthlib import OAuth1Session

    if not config.get("consumer_key") or not config.get("consumer_secret"):
        raise AdsError("app_keys_missing", "后台未配置 Lisa API 的应用密钥。", 503)
    return OAuth1Session(config["consumer_key"], client_secret=config["consumer_secret"],
                         resource_owner_key=token or None, resource_owner_secret=secret or None, **kwargs)


def token_request(session, endpoint):
    with session:
        try:
            response = session.post(X_API + endpoint, timeout=(5, 15), allow_redirects=False)
        except OSError:
            raise AdsError("x_unreachable", "暂时无法连接 X，请重新开始授权。", 502) from None
    if response.status_code != 200:
        raise AdsError("oauth_rejected", "X 拒绝本次授权请求，HTTP %d。" % response.status_code, 502)
    fields = {k: v[0] for k, v in parse_qs(response.text).items() if len(v) == 1}
    if not fields.get("oauth_token") or not fields.get("oauth_token_secret"):
        raise AdsError("oauth_incomplete", "X 的授权回执缺少凭据。", 502)
    return fields


@contextmanager
def database(data_dir):
    directory = Path(data_dir) / "ads-oauth"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "grants.sqlite3"
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    with closing(sqlite3.connect(str(path), timeout=10)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS pending (
            flow TEXT PRIMARY KEY, actor TEXT NOT NULL, browser TEXT NOT NULL, app TEXT NOT NULL,
            x_id TEXT NOT NULL, handle TEXT NOT NULL, secret TEXT NOT NULL, started INTEGER NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS grants (
            app TEXT NOT NULL, x_id TEXT NOT NULL, handle TEXT NOT NULL, token TEXT NOT NULL,
            secret TEXT NOT NULL, granted INTEGER NOT NULL, report TEXT, PRIMARY KEY (app,x_id))""")
        yield db


def actor_key(actor):
    return json.dumps([actor.get("tenant_key", ""), actor["user_id"]], separators=(",", ":"))


def start(data_dir, config, callback, actor, browser, channel):
    fields = token_request(signed(config, callback_uri=callback), "/oauth/request_token")
    if fields.get("oauth_callback_confirmed") != "true":
        raise AdsError("callback_unconfirmed", "X 未确认后台回调地址。", 502)
    now = int(time.time())
    with database(data_dir) as db:
        db.execute("DELETE FROM pending WHERE started < ?", (now - 900,))
        db.execute("INSERT INTO pending VALUES(?,?,?,?,?,?,?,?)",
                   (digest(fields["oauth_token"]), actor_key(actor), digest(browser), digest(config["consumer_key"]),
                    channel["x_user_id"], channel["username"], fields["oauth_token_secret"], now))
    return AUTHORIZE + "?" + urlencode({"oauth_token": fields["oauth_token"], "force_login": "true",
                                        "screen_name": channel["username"]})


def finish(data_dir, config, actor, browser, query):
    denied = query.get("denied", [""])[0]
    token = denied or query.get("oauth_token", [""])[0]
    verifier = query.get("oauth_verifier", [""])[0]
    if not token or (not denied and not verifier):
        raise AdsError("callback_incomplete", "X 授权回调缺少参数。")
    app = digest(config["consumer_key"])
    with database(data_dir) as db:
        db.execute("BEGIN IMMEDIATE")
        flow = db.execute("SELECT * FROM pending WHERE flow=? AND actor=? AND browser=? AND app=?",
                          (digest(token), actor_key(actor), digest(browser), app)).fetchone()
        if not flow or flow["started"] < time.time() - 900:
            raise AdsError("invalid_flow", "请用发起授权的浏览器返回后台，重新开始授权。", 403)
        db.execute("DELETE FROM pending WHERE flow=?", (flow["flow"],))
    if denied:
        raise AdsError("consent_declined", "已取消这次 X 授权。")
    fields = token_request(signed(config, token, flow["secret"], verifier=verifier), "/oauth/access_token")
    if fields.get("user_id") != flow["x_id"]:
        raise AdsError("different_x_account", "X 返回的账号与选定频道不同，请切换到 @" + flow["handle"] + " 后重新授权。", 409)
    with database(data_dir) as db:
        db.execute("""INSERT OR REPLACE INTO grants(app,x_id,handle,token,secret,granted,report)
                      VALUES(?,?,?,?,?,?,NULL)""",
                   (app, flow["x_id"], flow["handle"], fields["oauth_token"], fields["oauth_token_secret"], int(time.time())))
    return flow["x_id"], flow["handle"]


def status(data_dir, config, x_id):
    with database(data_dir) as db:
        row = db.execute("SELECT handle,granted,report FROM grants WHERE app=? AND x_id=?",
                         (digest(config.get("consumer_key", "")), x_id)).fetchone()
    return {"authorized": bool(row), "report": json.loads(row["report"]) if row and row["report"] else None}


def ads_read(session, path, params=None):
    try:
        response = session.get(ADS_API + path, params=params, timeout=(5, 12), allow_redirects=False)
    except OSError:
        return {"http": None, "error": "CONNECTION_FAILED"}, [], None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if response.status_code != 200 or not isinstance(body.get("data"), list):
        errors = body.get("errors")
        first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
        error = str(first.get("code") or "UNEXPECTED_RESPONSE")
        return {"http": response.status_code, "error": error if re.fullmatch(r"[A-Za-z0-9_]{1,70}", error) else "UPSTREAM_ERROR"}, [], None
    return {"http": response.status_code}, body["data"], body.get("next_cursor")


def ads_list(session, path):
    params = {"count": 200}
    entries, seen = [], set()
    while True:
        result, page, cursor = ads_read(session, path, params)
        entries.extend(page)
        if not cursor or "error" in result:
            return result, entries
        if not isinstance(cursor, str) or cursor in seen:
            return {"http": 200, "error": "INVALID_CURSOR"}, entries
        seen.add(cursor)
        params = {"count": 200, "cursor": cursor}


def probe(data_dir, config, x_id):
    app = digest(config["consumer_key"])
    with database(data_dir) as db:
        row = db.execute("SELECT token,secret FROM grants WHERE app=? AND x_id=?", (app, x_id)).fetchone()
    if not row:
        raise AdsError("grant_required", "请先完成频道的 X 用户授权。", 409)
    with signed(config, row["token"], row["secret"]) as session:
        result, accounts = ads_list(session, "/accounts")
        report = {"accounts_api": result, "accounts": []}
        for account in accounts:
            if not isinstance(account, dict):
                continue
            key = str(account.get("id", ""))
            if re.fullmatch(r"[A-Za-z0-9]+", key):
                report["accounts"].append({"id": key, "name": str(account.get("name") or "")[:180]})
        # The account list alone does not establish creative access. Check each
        # returned account until one confirms both this handle and readable cards.
        for account in report["accounts"]:
            base = "/accounts/" + account["id"]
            access, users = ads_list(session, base + "/promotable_users")
            account["promotable_users_api"] = access
            matched = next((u for u in users if isinstance(u, dict) and str(u.get("user_id")) == x_id), None)
            account["handle_access"] = str(matched.get("promotable_user_type", "")) if matched else None
            if account["handle_access"] == "FULL":
                account["cards_api"], _, _ = ads_read(session, base + "/cards", {"count": 1})
                if account["cards_api"] == {"http": 200}:
                    break
    report["checked"] = int(time.time())
    with database(data_dir) as db:
        db.execute("UPDATE grants SET report=? WHERE app=? AND x_id=? AND token=?",
                   (json.dumps(report, separators=(",", ":")), app, x_id, row["token"]))
    return report
