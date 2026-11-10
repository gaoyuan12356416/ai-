#!/usr/bin/env python3
"""Hourly, account-pool-bound statistics snapshot and HTTP API.

The HTTP process never connects to MySQL. Source reads belong to the refresh
command, and only a complete snapshot of the verified account scope is served.
"""

import argparse
import contextlib
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from decimal import Decimal, InvalidOperation, localcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit


# Historical URL segments do not filter products or pages.
API_PATH = "/api/v1/dramawave/w2a/2334/daily-stats"
VERSION = "1.5"
MIN_DAY = "2026-11-01"
REPORT_SCOPE = "account-pool-tiktok-campaign-delivery-custom-af0-from-20261101-v2"
DEFAULT_CONFIG = "/etc/dramawave-w2a-stats.json"
DEFAULT_STATE_DIR = "/mnt/data-disk/dramawave-w2a-stats"
DAY_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z", re.ASCII)
CENT = Decimal("0.01")


class ConfigError(ValueError):
    """A configuration is not usable; messages deliberately contain no secrets."""


class BusyError(RuntimeError):
    pass


def _error(code, message):
    return {"code": code, "message": message}


def _now(value=None):
    value = value() if callable(value) else value
    if value is None:
        value = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    if not isinstance(value, dt.datetime):
        raise ValueError("now must be a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.isoformat(timespec="seconds")


def load_config(path):
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ConfigError("Configuration is unavailable") from None
    if not isinstance(config, dict):
        raise ConfigError("Configuration must be an object")
    config = dict(config)
    pool_id = config.get("pool_id")
    if pool_id is not None:
        if isinstance(pool_id, bool) or not re.fullmatch(r"[1-9][0-9]*", str(pool_id), re.ASCII):
            raise ConfigError("Invalid pool configuration")
        pool_id = int(pool_id)
    config["pool_id"] = pool_id
    config.setdefault("state_dir", DEFAULT_STATE_DIR)
    config.setdefault("bind_host", "127.0.0.1")
    config.setdefault("port", 8850)
    config.setdefault("allowed_ips", [])
    config.setdefault("trusted_proxies", [])
    for key in ("state_dir", "token_file"):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
            raise ConfigError("Invalid file configuration")
    for key in ("allowed_ips", "trusted_proxies"):
        values = config[key]
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ConfigError("Invalid network configuration")
        try:
            for value in values:
                ipaddress.ip_network(value, strict=False)
        except ValueError:
            raise ConfigError("Invalid network configuration") from None
    if isinstance(config["port"], bool) or not isinstance(config["port"], int) or not 0 <= config["port"] <= 65535:
        raise ConfigError("Invalid listen port")
    try:
        if not ipaddress.ip_address(config["bind_host"]).is_loopback:
            raise ValueError()
    except (TypeError, ValueError):
        raise ConfigError("HTTP listener must use a loopback address") from None
    return config


def _token(config):
    try:
        value = Path(config["token_file"]).read_text(encoding="utf-8").strip()
    except OSError:
        raise ConfigError("Token is not configured") from None
    if not value or any(char.isspace() for char in value):
        raise ConfigError("Token is not configured")
    return value


def _address(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _in_networks(address, networks):
    for value in networks:
        network = ipaddress.ip_network(value, strict=False)
        if address.version == network.version and address in network:
            return True
    return False


def client_address(peer_ip, headers, trusted_proxies):
    """Strip only trusted right-hand hops; untrusted peers cannot supply XFF."""
    address = _address(peer_ip)
    if not _in_networks(address, trusted_proxies):
        return address
    forwarded = headers.get("x-forwarded-for", "")
    if not forwarded:
        return address
    # Every component is checked, so malformed chains never silently widen access.
    parts = forwarded.split(",")
    if len(parts) > 64:
        raise ValueError("Invalid forwarding chain")
    chain = [_address(part.strip()) for part in parts]
    for candidate in reversed(chain):
        if not _in_networks(address, trusted_proxies):
            break
        address = candidate
    return address


def _headers(headers):
    normalized = {}
    for key, value in headers.items():
        key = str(key).lower()
        if key not in ("authorization", "x-forwarded-for"):
            continue
        if key in normalized:
            raise ValueError("Duplicate header")
        normalized[key] = str(value)
    return normalized


def authorize(config, peer_ip, headers):
    try:
        headers = _headers(headers)
    except ValueError:
        return 401, _error("UNAUTHORIZED", "Token 缺失或无效")
    try:
        expected = _token(config)
    except ConfigError:
        return 503, _error("CONFIG_NOT_READY", "访问凭据尚未配置")
    authorization = headers.get("authorization", "")
    pieces = authorization.split()
    if len(pieces) != 2 or pieces[0].lower() != "bearer" or not hmac.compare_digest(
        pieces[1].encode("utf-8"), expected.encode("utf-8")
    ):
        return 401, _error("UNAUTHORIZED", "Token 缺失或无效")
    try:
        address = client_address(peer_ip, headers, config["trusted_proxies"])
        allowed = _in_networks(address, config["allowed_ips"])
    except ValueError:
        allowed = False
    if not allowed:
        return 403, _error("IP_NOT_ALLOWED", "IP 未获允许")
    return None


def parse_dates(query):
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
    except ValueError:
        raise ValueError("Invalid date parameters") from None
    if len(pairs) != 2 or {key for key, _ in pairs} != {"start_date", "end_date"}:
        raise ValueError("Invalid date parameters")
    values = dict(pairs)
    for value in values.values():
        if not DAY_PATTERN.fullmatch(value):
            raise ValueError("Invalid date parameters")
        dt.date.fromisoformat(value)
    if values["start_date"] > values["end_date"]:
        raise ValueError("Invalid date parameters")
    return values["start_date"], values["end_date"]


def _scope(pool_id, accounts):
    if isinstance(accounts, (str, bytes)) or accounts is None:
        raise ValueError("Invalid member list")
    members = []
    for value in accounts:
        if value is None or isinstance(value, bool):
            raise ValueError("Invalid member identity")
        member = str(value)
        if not member or member != member.strip() or len(member) > 255:
            raise ValueError("Invalid member identity")
        members.append(member)
    members = tuple(sorted(set(members)))
    encoded = json.dumps([REPORT_SCOPE, pool_id, members],
                         separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest(), members


def _current_snapshot_scope(state, pool_id):
    """An older product/page-filtered snapshot is not the current pool report."""
    try:
        expected, _ = _scope(pool_id, json.loads(state["members_json"]))
        return state["scope_id"] == expected and state["snapshot_scope"] == expected
    except (TypeError, ValueError, KeyError):
        return False


def _amount(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Invalid amount")
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError("Invalid amount")
        with localcontext() as context:
            context.prec = 60
            rounded = number.quantize(CENT)
        if rounded != number:
            raise ValueError("Source amount has unexpected precision")
        return rounded
    except InvalidOperation:
        raise ValueError("Invalid amount") from None


def _integer(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Invalid count")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError("Invalid count")
        integer = int(number)
        if not -(2 ** 63) <= integer < 2 ** 63:
            raise ValueError("Count outside storage range")
        return integer
    except InvalidOperation:
        raise ValueError("Invalid count") from None


def normalize_rows(rows):
    normalized = []
    seen = set()
    fields = {"day", "impression", "click", "install", "revenue", "cost"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError("Invalid source row")
        day = row["day"].isoformat() if isinstance(row["day"], dt.date) else row["day"]
        if not isinstance(day, str) or not DAY_PATTERN.fullmatch(day):
            raise ValueError("Invalid source date")
        dt.date.fromisoformat(day)
        if day in seen:
            raise ValueError("Duplicate source day")
        seen.add(day)
        normalized.append({
            "day": day,
            "impression": _integer(row["impression"]),
            "click": _integer(row["click"]),
            "install": _integer(row["install"]),
            "revenue": _amount(row["revenue"]),
            "cost": _amount(row["cost"]),
        })
    return sorted(normalized, key=lambda row: row["day"])


class Store:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "snapshot.sqlite3"

    def _connect(self, write=False):
        if write:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path), timeout=5)
        else:
            if not self.path.is_file():
                raise FileNotFoundError()
            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self):
        with contextlib.closing(self._connect(write=True)) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY CHECK (id=1), pool_id TEXT, scope_id TEXT,
                members_json TEXT, scope_valid INTEGER NOT NULL DEFAULT 0,
                snapshot_scope TEXT, updated_at TEXT, stale INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT, last_error TEXT
            )""")
            connection.execute("INSERT OR IGNORE INTO state(id) VALUES(1)")
            connection.execute("""CREATE TABLE IF NOT EXISTS daily (
                day TEXT PRIMARY KEY, impression INTEGER NOT NULL, click INTEGER NOT NULL,
                install INTEGER NOT NULL, revenue TEXT NOT NULL, cost TEXT NOT NULL
            )""")

    def inspect(self):
        try:
            with contextlib.closing(self._connect()) as connection:
                row = connection.execute("SELECT * FROM state WHERE id=1").fetchone()
                return dict(row) if row else None
        except (FileNotFoundError, sqlite3.Error):
            return None

    def invalidate(self, pool_id, timestamp, reason):
        with contextlib.closing(self._connect(write=True)) as connection, connection:
            connection.execute("""UPDATE state SET pool_id=?, scope_valid=0,
                stale=1, last_attempt_at=?, last_error=? WHERE id=1""",
                (str(pool_id) if pool_id is not None else None, timestamp, reason))

    def begin_scope(self, pool_id, scope_id, members, timestamp):
        with contextlib.closing(self._connect(write=True)) as connection, connection:
            connection.execute("""UPDATE state SET pool_id=?, scope_id=?, members_json=?,
                scope_valid=1, stale=1, last_attempt_at=?, last_error=NULL WHERE id=1""",
                (str(pool_id), scope_id, json.dumps(members), timestamp))

    def fail_metrics(self, timestamp):
        with contextlib.closing(self._connect(write=True)) as connection, connection:
            connection.execute("""UPDATE state SET stale=1, last_attempt_at=?,
                last_error='source_read_failed' WHERE id=1""", (timestamp,))

    def publish(self, scope_id, rows, timestamp):
        with contextlib.closing(self._connect(write=True)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute("SELECT scope_id,scope_valid FROM state WHERE id=1").fetchone()
            if not state or not state["scope_valid"] or state["scope_id"] != scope_id:
                raise ValueError("Scope changed during refresh")
            connection.execute("DELETE FROM daily")
            connection.executemany("INSERT INTO daily VALUES(?,?,?,?,?,?)", [
                (row["day"], row["impression"], row["click"], row["install"],
                 format(row["revenue"], ".2f"), format(row["cost"], ".2f")) for row in rows
            ])
            connection.execute("""UPDATE state SET snapshot_scope=?, updated_at=?, stale=0,
                last_attempt_at=?, last_error=NULL WHERE id=1""", (scope_id, timestamp, timestamp))

    def read(self, start_date, end_date, pool_id):
        try:
            with contextlib.closing(self._connect()) as connection:
                connection.execute("BEGIN")
                state = connection.execute("SELECT * FROM state WHERE id=1").fetchone()
                if not state:
                    return 503, _error("DATA_NOT_READY", "尚无成功数据快照")
                if state["pool_id"] != str(pool_id):
                    return 503, _error("DATA_NOT_READY", "当前账户池尚无成功数据快照")
                if not state["scope_valid"]:
                    return 503, _error("CONFIG_NOT_READY", "账户池范围暂不可用")
                if not state["updated_at"] or not _current_snapshot_scope(state, pool_id):
                    return 503, _error("DATA_NOT_READY", "当前账户池范围尚无成功数据快照")
                rows = connection.execute("SELECT * FROM daily WHERE day>=? AND day<=? ORDER BY day",
                                          (max(start_date, MIN_DAY), end_date)).fetchall()
                data = []
                for row in rows:
                    item = dict(row)
                    item["revenue"] = Decimal(item["revenue"])
                    item["cost"] = Decimal(item["cost"])
                    data.append(item)
                return 200, {"code": "OK", "message": "success", "updated_at": state["updated_at"],
                             "stale": bool(state["stale"]), "data": data}
        except (FileNotFoundError, sqlite3.Error, InvalidOperation):
            return 503, _error("DATA_NOT_READY", "数据快照暂不可用")


@contextlib.contextmanager
def refresh_lock(state_dir):
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stream = (directory / "refresh.lock").open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise BusyError("Refresh is already running") from None
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BusyError("Refresh is already running") from None
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def refresh(config_path, source=None, now=None):
    try:
        config = load_config(config_path)
    except ConfigError:
        return {"status": "config_not_ready", "reason": "invalid_configuration"}
    timestamp = _now(now)
    store = Store(config["state_dir"])
    try:
        with refresh_lock(config["state_dir"]):
            store.initialize()
            if config["pool_id"] is None:
                store.invalidate(None, timestamp, "pool_not_configured")
                return {"status": "config_not_ready", "reason": "pool_not_configured"}
            try:
                if source is None:
                    from source import MySQLSource
                    source = MySQLSource(config)
                scope_id, members = _scope(config["pool_id"], source.load_scope(config))
            except Exception:
                store.invalidate(config["pool_id"], timestamp, "scope_unavailable")
                return {"status": "config_not_ready", "reason": "scope_unavailable"}
            store.begin_scope(config["pool_id"], scope_id, members, timestamp)
            try:
                rows = normalize_rows(source.read_daily(members)) if members else []
            except Exception:
                store.fail_metrics(timestamp)
                return {"status": "failed", "reason": "source_read_failed"}
            try:
                current = load_config(config_path)
                if current["pool_id"] != config["pool_id"] or current["state_dir"] != config["state_dir"]:
                    store.invalidate(current["pool_id"], timestamp, "configuration_changed")
                    return {"status": "config_not_ready", "reason": "configuration_changed"}
                checked_scope, checked_members = _scope(current["pool_id"], source.load_scope(current))
                if checked_scope != scope_id:
                    store.begin_scope(current["pool_id"], checked_scope, checked_members, timestamp)
                    return {"status": "failed", "reason": "scope_changed"}
            except Exception:
                store.invalidate(config["pool_id"], timestamp, "scope_unavailable")
                return {"status": "config_not_ready", "reason": "scope_unavailable"}
            completed_at = _now(now)
            store.publish(scope_id, rows, completed_at)
            return {"status": "ok", "rows": len(rows), "updated_at": completed_at}
    except BusyError:
        return {"status": "busy"}
    except (OSError, sqlite3.Error, ValueError):
        return {"status": "failed", "reason": "snapshot_write_failed"}


def query_response(config_path, query, peer_ip, headers, now=None):
    del now  # The API exposes the stored success timestamp, never request time.
    try:
        config = load_config(config_path)
    except ConfigError:
        return 503, _error("CONFIG_NOT_READY", "接口配置暂不可用")
    rejection = authorize(config, peer_ip, headers)
    if rejection:
        return rejection
    try:
        start_date, end_date = parse_dates(query)
    except ValueError:
        return 400, _error("INVALID_PARAMETER", "请提供有效的 start_date 和 end_date")
    if config["pool_id"] is None:
        return 503, _error("CONFIG_NOT_READY", "账户池尚未配置")
    return Store(config["state_dir"]).read(start_date, end_date, config["pool_id"])


def dumps(value):
    """Serialize validated Decimals as JSON numbers without float conversion."""
    if isinstance(value, Decimal):
        return format(_amount(value), ".2f")
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(str(key), ensure_ascii=False) + ":" + dumps(item)
                              for key, item in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(dumps(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def health(config_path):
    result = {"service": "dramawave-w2a-stats", "version": VERSION,
              "config_ready": False, "has_snapshot": False}
    try:
        config = load_config(config_path)
        _token(config)
        result["config_ready"] = config["pool_id"] is not None and bool(config["allowed_ips"])
        state = Store(config["state_dir"]).inspect()
        if state:
            result["has_snapshot"] = bool(state["scope_valid"] and state["updated_at"]
                                          and state["pool_id"] == str(config["pool_id"])
                                          and _current_snapshot_scope(state, config["pool_id"]))
    except ConfigError:
        pass
    return result


def make_handler(config_path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "W2AStats/1.4"
        sys_version = ""

        def _send(self, status, payload):
            body = dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            try:
                parsed = urlsplit(self.path)
                if parsed.path == "/healthz" and not parsed.query:
                    if not _address(self.client_address[0]).is_loopback:
                        self._send(403, _error("IP_NOT_ALLOWED", "IP 未获允许"))
                    else:
                        self._send(200, health(config_path))
                    return
                if parsed.path != API_PATH:
                    self._send(404, _error("NOT_FOUND", "接口不存在"))
                    return
                status, payload = query_response(config_path, parsed.query, self.client_address[0], self.headers)
                self._send(status, payload)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                self._send(503, _error("DATA_NOT_READY", "接口暂不可用"))

        def _method_not_allowed(self):
            self._send(405, _error("METHOD_NOT_ALLOWED", "仅支持 GET"))

        do_POST = _method_not_allowed
        do_PUT = _method_not_allowed
        do_DELETE = _method_not_allowed
        do_PATCH = _method_not_allowed
        do_HEAD = _method_not_allowed
        do_OPTIONS = _method_not_allowed

        def log_message(self, fmt, *args):
            # Never include request headers, tokens, query strings or source errors.
            if len(args) > 1 and str(args[1]).isdigit():
                sys.stderr.write("w2a_http status=" + str(args[1]) + "\n")

    return Handler


def make_server(config_path):
    config = load_config(config_path)
    server_type = ThreadingHTTPServer
    if _address(config["bind_host"]).version == 6:
        import socket
        class IPv6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server_type = IPv6Server
    server = server_type((config["bind_host"], config["port"]), make_handler(config_path))
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("serve", "refresh"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    if args.command == "refresh":
        result = refresh(args.config)
        print(dumps(result))
        if result["status"] in ("ok", "busy"):
            return 0
        # A deliberately unconfigured pool is a parked installation, while an
        # unreadable/invalid configured pool must make the hourly unit fail.
        return 0 if result.get("reason") == "pool_not_configured" else 1
    try:
        server = make_server(args.config)
    except (ConfigError, OSError):
        print("HTTP service configuration is unavailable", file=sys.stderr)
        return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
