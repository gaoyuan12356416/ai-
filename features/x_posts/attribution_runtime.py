"""Main API only: read-only publisher identity resolution, no publisher imports."""
import hmac
import json
import os
import re
import subprocess
from .attribution import AttributionError, Resolver


def resolver(app):
    def query(statement):
        if str(app["ADMIN_MAPPING_MYSQL_HOST"]) != "101.32.56.53" or str(app["ADMIN_MAPPING_MYSQL_PORT"]) != "63350":
            raise AttributionError("x_attribution_replica_invalid", 503)
        env = dict(os.environ)
        env.pop("SQL_GATE_BYPASS", None)
        env.update(MYSQL_PWD=app["ADMIN_MAPPING_MYSQL_PASSWORD"],
                   SQL_GATE_JOB_NAME="x-attribution", SQL_GATE_WAIT_TIMEOUT_SECONDS="5",
                   SQL_GATE_EXEC_TIMEOUT_SECONDS="12")
        sql = "SET SESSION MAX_EXECUTION_TIME=8000; START TRANSACTION READ ONLY; SELECT @@read_only;" + statement + ";ROLLBACK;"
        try:
            result = subprocess.run(["/usr/bin/mysql", "-h", "101.32.56.53", "-P", "63350",
                                     "-u", str(app["ADMIN_MAPPING_MYSQL_USER"]), "--connect-timeout=3",
                                     "--skip-reconnect", "--default-character-set=utf8mb4", "-N", "-B", "-r"],
                                    input=sql, env=env, text=True, encoding="utf-8",
                                    capture_output=True, timeout=25)
            rows = result.stdout.splitlines()
            if result.returncode or not rows or rows.pop(0) != "1":
                raise ValueError()
            return rows
        except Exception:
            raise AttributionError("x_attribution_mapping_unavailable", 503) from None
    database = str(app["ADMIN_MAPPING_MYSQL_DATABASE"])
    if not re.fullmatch(r"[A-Za-z0-9_]+", database):
        raise AttributionError("x_attribution_replica_invalid", 503)
    def ids_for_email(email):
        return query("SELECT DISTINCT sub_user_id FROM `" + database + "`.admin_user_group "
                     "WHERE BINARY LOWER(TRIM(email))=BINARY CONVERT(0x" + email.encode().hex() +
                     " USING utf8mb4) LIMIT 2")
    def emails_for_id(sub_id):
        rows = query("SELECT DISTINCT HEX(LOWER(TRIM(email))) FROM `" + database +
                     "`.admin_user_group WHERE sub_user_id=" + str(int(sub_id)) + " LIMIT 101")
        if len(rows) > 100:
            raise AttributionError("x_attribution_default_owner_ambiguous")
        return [bytes.fromhex(v).decode("utf-8") for v in rows if v and v != "NULL"]
    return Resolver(app.get("SOCIAL_ANALYTICS_X_DB_PATH", "/var/lib/x-post-automation/accounts.sqlite3"),
                    app.get("X_ATTRIBUTION_AUTO_DB_PATH", "/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3"),
                    app["JOB_DB_PATH"], ids_for_email, emails_for_id)


def dispatch(handler, app):
    reply = lambda status, data: app["json_response"](handler, status, data, no_store=True)
    token = str(os.environ.get("X_POST_AUTOMATION_INTERNAL_TOKEN") or "").strip()
    supplied = str(handler.headers.get("Authorization") or "")
    if (handler.client_address[0] not in {"127.0.0.1", "::1"} or len(token) < 32
            or not hmac.compare_digest(supplied, "Bearer " + token)):
        return reply(403, dict(error="permission_denied"))
    try:
        body = handler._read_json()
        if not isinstance(body, dict) or set(body) != {"queue_id", "log_id"}:
            return reply(400, dict(error="invalid_request"))
        return reply(200, dict(context=resolver(app).resolve(body["queue_id"], body["log_id"])))
    except AttributionError as exc:
        return reply(exc.status_code, dict(error=exc.code))
    except Exception:
        return reply(503, dict(error="x_attribution_resolution_unavailable"))
