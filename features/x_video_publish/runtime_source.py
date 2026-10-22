"""Manual publisher reads use the host FIFO gate, never the reserved SQL pool."""
from __future__ import annotations

import os
import json
import re
import signal
import subprocess
import threading
import xml.etree.ElementTree as ET
from decimal import Decimal

from .errors import PublishingError as TemplateError

SQL_SLOTS = threading.BoundedSemaphore(2)
MAX_RESULT_BYTES = 16 * 1024 * 1024


class GatedReadOnlyMySQL:
    """Drop-in SELECT adapter; credentials and token results stay in memory."""

    def __init__(self, environ=None):
        env = os.environ if environ is None else environ
        self.schema = str(env.get("X_VIDEO_PUBLISH_MYSQL_DATABASE", "kunlunads_dev"))
        self.blacklist_schema = str(env.get("X_VIDEO_PUBLISH_UNUSED_SCHEMA", "ads_setting"))
        self._user = str(env.get("X_VIDEO_PUBLISH_MYSQL_USER", ""))
        self._password = str(env.get("X_VIDEO_PUBLISH_MYSQL_PASSWORD", ""))
        if (not self._user or not self._password or any(
                not re.fullmatch(r"[A-Za-z0-9_]+", value)
                for value in (self.schema, self.blacklist_schema))):
            raise TemplateError("source_not_configured", "素材只读数据源配置无效", 503)

    @staticmethod
    def _bind(sql, params):
        # All SQL templates belong to the repositories, not request input.
        if (not isinstance(sql, str) or not re.match(r"(?is)^\s*SELECT\b", sql)
                or ";" in sql or re.search(r"(?i)\b(?:OUTFILE|DUMPFILE)\b", sql)):
            raise TemplateError("source_query_rejected", "只读数据源拒绝了无效查询", 500)
        parts = sql.split("%s")
        if len(parts) != len(params) + 1:
            raise TemplateError("source_query_rejected", "只读查询参数无效", 500)
        values = []
        for value in params:
            if value is None:
                literal = "NULL"
            elif isinstance(value, bool):
                literal = "1" if value else "0"
            elif isinstance(value, int):
                literal = str(value)
            elif isinstance(value, Decimal) and value.is_finite():
                literal = str(value)
            elif isinstance(value, str):
                # No quoting/backslash/SQL-mode ambiguity, including Unicode.
                encoded = value.encode("utf-8").hex()
                # A charset-introduced literal stays COERCIBLE. CONVERT(hex)
                # becomes IMPLICIT and conflicts with unicode_ci DB columns.
                literal = "_utf8mb4 0x" + encoded if encoded else "''"
            else:
                raise TemplateError("source_query_rejected", "只读查询参数类型无效", 500)
            values.append(literal)
        return "".join(part + (values[index] if index < len(values) else "")
                       for index, part in enumerate(parts))

    @staticmethod
    def _decode(output):
        if len(output) > MAX_RESULT_BYTES:
            raise TemplateError("source_result_too_large", "数据源返回超过安全范围", 503)
        try:
            xml = re.sub(br"<\?xml[^?]*\?>", b"", output)
            root = ET.fromstring(b"<results>" + xml + b"</results>")
            sets = list(root)
            if len(sets) != 2 or any(item.tag != "resultset" for item in sets):
                raise ValueError()
            check = sets[0].findall("row/field")
            if len(check) != 1 or check[0].get("name") != "replica_read_only" or check[0].text != "1":
                raise ValueError()
            rows = []
            for row in sets[1].findall("row"):
                item = {}
                for field in row.findall("field"):
                    key = field.get("name")
                    if not key or key in item:
                        raise ValueError()
                    nil = field.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true"
                    item[key] = None if nil else field.text or ""
                rows.append(item)
                if len(rows) > 20000:
                    raise ValueError()
            return rows
        except (ET.ParseError, ValueError, UnicodeError):
            raise TemplateError("source_readonly_check_failed", "只读副本校验或数据格式无效", 503) from None

    def select(self, sql, params):
        bound = self._bind(sql, params)
        if not SQL_SLOTS.acquire(timeout=2):
            raise TemplateError("source_busy", "素材查询繁忙，请稍后重试", 503)
        process = None
        try:
            env = os.environ.copy()
            env.pop("SQL_GATE_BYPASS", None)
            env["MYSQL_PWD"] = self._password
            statement = (
                "SET SESSION MAX_EXECUTION_TIME=8000; START TRANSACTION READ ONLY; "
                "SELECT @@read_only AS replica_read_only; " + bound + "; ROLLBACK;"
            )
            command = [
                "/usr/bin/mysql", "-h", "101.32.56.53", "-P", "63350", "-u", self._user,
                "--default-character-set=utf8mb4", "--connect-timeout=3",
                "--xml", "--batch", "--execute", statement,
            ]
            # The host /usr/bin/mysql is the shared FIFO wrapper. Do not use
            # mysql.real or pymysql for this application adapter.
            process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            output, _ = process.communicate(timeout=25)
            if process.returncode:
                raise ValueError()
            return self._decode(output)
        except TemplateError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            # Never include command, stderr or SQL/token output in exceptions.
            raise TemplateError("source_query_failed", "只读数据查询暂不可用，请稍后重试", 503) from None
        finally:
            if process is not None and process.poll() is None:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.communicate()
            SQL_SLOTS.release()

    def iter_select(self, sql, params, *, batch_size=1000):
        if type(batch_size) is not int or not 1 <= batch_size <= 5000:
            raise TemplateError("source_query_rejected", "只读查询批量大小无效", 500)
        yield from self.select(sql, params)
