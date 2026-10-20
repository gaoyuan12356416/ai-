"""Private root API bridge to the existing root-only SQL FIFO gate.

The FB sidecar stays unprivileged. No database passwords cross this endpoint.
Nginx rejects this exact path; direct callers also need loopback and the existing
internal bearer. Only this feature's fixed source tables may be selected.
"""
import hmac
import json
import os
import re

from .runtime_source import GatedReadOnlyMySQL
from .templates import TemplateError

PATH = "/internal/fb-video-publish/query"
SCHEMA = "kunlunads_dev"
BLACKLIST_SCHEMA = "ads_setting"
TABLES = {
    "ads_facebook_page_group", "ads_apps_setting", "ads_facebook_page_group_ins",
    "ads_facebook_page_post", "ads_facebook_post_publish_queue", "ads_custom_source",
    "ads_drama_info", "ads_facebook_post_blacklist", "ads_drama_resource",
}

_TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<hint>/\*\+\s*MAX_EXECUTION_TIME\s*\(\s*8000\s*\)\s*\*/)"
    r"|(?P<string>'(?:''|\\.|[^'\\])*')|(?P<quoted>`[A-Za-z0-9_]+`)"
    r"|(?P<parameter>%s)|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<number>[0-9]+(?:\.[0-9]+)?)|(?P<symbol><>|<=|>=|!=|[().,=*+/<>\-])",
    re.IGNORECASE,
)
_FORBIDDEN = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "UNION", "INTO", "OUTFILE", "DUMPFILE", "LOAD_FILE", "SLEEP", "BENCHMARK", "GET_LOCK",
    "LOCK", "FOR", "PROCEDURE", "TABLE", "WITH", "INTERSECT", "EXCEPT",
}
_FUNCTIONS = {"CAST", "TRIM", "COALESCE", "LOWER", "REPLACE", "MAX", "COUNT", "LOCATE", "CONCAT"}
_PAREN_KEYWORDS = {"EXISTS", "IN", "INDEX", "AND", "OR", "NOT"}


def _tokens(sql):
    """Lex the small repository SQL dialect; every byte must be recognized.

    Only the exact execution-time hint is a permitted comment. Quoted strings
    are indivisible, so their text never becomes a table/keyword or bypass.
    """
    result, position = [], 0
    while position < len(sql):
        match = _TOKEN.match(sql, position)
        if match is None or (sql.startswith("/*", position) and match.lastgroup != "hint"):
            raise ValueError("Unsupported SQL syntax")
        kind, value = match.lastgroup, match.group()
        if kind not in {"space", "hint"}:
            result.append((kind, value))
        position = match.end()
    return result


def _validate_sources(sql):
    """Allow qualified base tables and explicit joins, not general SQL."""
    tokens = _tokens(sql)
    if not tokens or tokens[0][0] != "word" or tokens[0][1].upper() != "SELECT":
        raise ValueError("Expected SELECT")
    depth, from_depths, source_count = 0, set(), 0
    for index, (kind, value) in enumerate(tokens):
        word = value.upper() if kind == "word" else ""
        if word in _FORBIDDEN:
            raise ValueError("Unsupported SQL operation")
        if value == "(" and kind == "symbol":
            depth += 1
        elif value == ")" and kind == "symbol":
            from_depths.discard(depth)
            depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced SQL")
        elif value == "," and kind == "symbol" and depth in from_depths:
            # Repository joins are explicit. Comma table lists would require
            # another parser branch and are deliberately outside this dialect.
            raise ValueError("Comma table lists are not supported")
        qualified = index > 0 and tokens[index - 1] == ("symbol", ".")
        clause = word in {"WHERE", "HAVING", "LIMIT"} or (
            word in {"GROUP", "ORDER"} and index + 1 < len(tokens)
            and tokens[index + 1][0] == "word" and tokens[index + 1][1].upper() == "BY")
        if clause and not qualified:
            from_depths.discard(depth)
        if word in {"FROM", "JOIN", "STRAIGHT_JOIN"}:
            source = tokens[index + 1:index + 4]
            if (len(source) != 3 or source[0][0] != "quoted"
                    or source[1] != ("symbol", ".") or source[2][0] != "word"):
                raise ValueError("Expected qualified base table")
            schema, table = source[0][1][1:-1], source[2][1]
            expected_schema = BLACKLIST_SCHEMA if table == "ads_facebook_post_blacklist" else SCHEMA
            if schema != expected_schema or table not in TABLES:
                raise ValueError("Unapproved source table")
            if tokens[index + 4:index + 5] == [("symbol", ".")]:
                raise ValueError("Invalid table qualification")
            from_depths.add(depth)
            source_count += 1
        if kind == "word" and tokens[index + 1:index + 2] == [("symbol", "(")]:
            if word not in _FUNCTIONS | _PAREN_KEYWORDS:
                raise ValueError("Unapproved SQL function")
            if index and tokens[index - 1] == ("symbol", "."):
                raise ValueError("Qualified functions are not supported")
        if kind == "quoted" and tokens[index + 1:index + 2] == [("symbol", "(")]:
            raise ValueError("Quoted functions are not supported")
    if depth or not source_count:
        raise ValueError("Invalid SELECT source")


def validate_query(payload):
    if not isinstance(payload, dict) or set(payload) != {"sql", "params", "schema", "blacklist_schema"}:
        raise ValueError("Invalid shape")
    if payload["schema"] != SCHEMA or payload["blacklist_schema"] != BLACKLIST_SCHEMA:
        raise ValueError("Invalid schema")
    sql, params = payload["sql"], payload["params"]
    if (not isinstance(sql, str) or len(sql) > 65536 or not isinstance(params, list)
            or len(params) > 2000 or not re.match(r"(?is)^\s*SELECT\b", sql)
            or ";" in sql or "--" in sql or "#" in sql):
        raise ValueError("Invalid query")
    _validate_sources(sql)
    return sql, params


def dispatch(handler, app):
    respond = lambda status, payload: app["json_response"](handler, status, payload, no_store=True)
    expected = str(os.environ.get("FB_AUTO_POST_INTERNAL_TOKEN", ""))
    auth = str(handler.headers.get("Authorization", ""))
    remote = str(handler.client_address[0])
    if (remote not in {"127.0.0.1", "::1"} or handler.headers.get("X-Forwarded-For")
            or not re.fullmatch(r"[A-Za-z0-9._~-]{32,512}", expected)
            or not hmac.compare_digest(auth, "Bearer " + expected)):
        handler.close_connection = True
        respond(403, {"ok": False, "code": "forbidden", "message": "Internal route"})
        return
    try:
        lengths = handler.headers.get_all("Content-Length", [])
        if (handler.headers.get("Transfer-Encoding") or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,6}", lengths[0])
                or not 2 <= int(lengths[0]) <= 256 * 1024
                or handler.headers.get("Content-Type", "").split(";", 1)[0] != "application/json"):
            raise ValueError("Invalid request")
        raw = handler.rfile.read(int(lengths[0]))
        if len(raw) != int(lengths[0]):
            raise ValueError("Invalid body")
        sql, params = validate_query(json.loads(raw.decode("utf-8")))
        trusted = {
            "FB_AUTO_MYSQL_USER": str(app["MYSQL_USER"]),
            "FB_AUTO_MYSQL_PASSWORD": str(app["MYSQL_PASSWORD"]),
            "FB_AUTO_MYSQL_DATABASE": SCHEMA,
            "FB_AUTO_BLACKLIST_MYSQL_DATABASE": BLACKLIST_SCHEMA,
        }
        items = GatedReadOnlyMySQL(trusted).select(sql, params)
        respond(200, {"ok": True, "items": items})
    except (ValueError, UnicodeError, KeyError, TypeError):
        handler.close_connection = True
        respond(400, {"ok": False, "code": "source_query_rejected", "message": "内部只读请求无效"})
    except Exception:
        # Never print a subprocess's SQL/credentials/output or raw exception.
        respond(503, {"ok": False, "code": "source_query_failed", "message": "只读数据源暂不可用"})
