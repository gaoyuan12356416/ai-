"""Bounded, redacted MySQL stderr; never capture query results or credentials."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import traceback
import uuid


LIMIT = 65536


def redact(text, secrets=()):
    for secret in sorted((str(v) for v in secrets if v), key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    text = re.sub(r"(?i)(?:_utf8mb4\s+)?0x[0-9a-f]+", '[HEX]', text)
    text = re.sub(r"(?s)'(?:\\.|''|[^'\\])*'|\"(?:\\.|\"\"|[^\"\\])*\"", "'[REDACTED]'", text)
    text = re.sub(r'(?i)((?:password|passwd|access_?token|refresh_?token|app_secret|authorization|api[_-]?key)\s*[:=]\s*)\S+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)Bearer\s+\S+|EA[A-Za-z0-9]{30,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED]', text)
    return ''.join(c for c in text if c in '\n\t' or ord(c) >= 32)


def query_shape(sql):
    return re.sub(r'\b\d+\b', '?', redact(sql))[:12000]


class StderrCapture:
    def __init__(self, stream, secrets=()):
        self.stream, self.secrets = stream, secrets
        self.text = ''
        self.truncated = False
        self.read_failed = False
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _append(self, line):
        with self.lock:
            self.text += line
            encoded = self.text.encode('utf-8')
            if len(encoded) > LIMIT:
                # Drop complete sanitized lines, never retain a partial secret.
                self.truncated = True
                self.text = encoded[-LIMIT:].decode('utf-8', errors='ignore')
                self.text = self.text.partition('\n')[2]

    def _read(self):
        skipping = False
        try:
            while True:
                line = self.stream.readline(4096)
                if not line:
                    break
                if len(line) == 4096 and not line.endswith('\n'):
                    if not skipping:
                        self._append('[overlong stderr line omitted]\n')
                    skipping = self.truncated = True
                    continue
                if skipping:
                    skipping = False
                    continue
                self._append(redact(line, self.secrets))
        except (OSError, ValueError, UnicodeError):
            self.read_failed = True

    def finish(self):
        self.thread.join(timeout=2)
        with self.lock:
            return self.text, self.truncated, self.read_failed or self.thread.is_alive()


def record_failure(source, session, sql, code, elapsed, partial_rows, returncode_before_close):
    stderr, truncated, read_failed = session.stderr_capture.finish() if session.stderr_capture else ('', False, False)
    errors = [{'errno': int(m.group(1)), 'sqlstate': m.group(2)}
              for m in re.finditer(r'ERROR\s+(\d+)\s+\(([A-Z0-9]{5})\)', stderr)]
    record = {
        'id': uuid.uuid4().hex, 'at': datetime.now(timezone.utc).isoformat(),
        'code': code, 'mysql_errors': errors, 'stderr': stderr,
        'stderr_truncated': truncated, 'stderr_incomplete': read_failed,
        'returncode_before_close': returncode_before_close,
        'returncode': session.proc.poll() if session.proc else None,
        'elapsed_seconds': round(elapsed, 3), 'partial_rows_discarded': partial_rows,
        'query_sequence': source._metrics['query_count'],
        'query_sha256': hashlib.sha256(sql.encode('utf-8')).hexdigest(),
        'query_shape': query_shape(sql),
        'callsite': [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
                     for f in traceback.extract_stack()[:-1]
                     if 'meta_violation_cleanup' in Path(f.filename).parts][-10:],
    }
    source.last_diagnostic = record
    if source.diagnostic_path:
        try:
            path = Path(source.diagnostic_path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(record, ensure_ascii=True) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            # Keep the original SQL failure; callers can still save the safe ID/code.
            record['diagnostic_write_failed'] = True
