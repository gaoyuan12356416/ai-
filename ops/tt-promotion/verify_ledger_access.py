"""Regression check on a systemd host with the production SQLite build (run as root)."""

import configparser
import os
from pathlib import Path
import pwd
import sqlite3
import subprocess
import tempfile


UNITS = Path(__file__).resolve().parent
LEGACY = Path("/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3")
READ_TEST = """
import os, sqlite3, sys
from pathlib import Path
p = Path(sys.argv[1])
assert os.statvfs(p).f_flag & os.ST_RDONLY, 'legacy database must remain read-only'
assert not Path(str(p) + '-shm').exists(), 'fixture must start without a coordination file'
with sqlite3.connect(p.as_uri() + '?mode=ro', uri=True) as db:
    db.execute('PRAGMA query_only=ON')
    assert db.execute('SELECT v FROM sample').fetchone() == (7,)
"""


def check(unit):
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.read(UNITS / unit)
    service = config["Service"]
    user = pwd.getpwnam(service["User"])
    with tempfile.TemporaryDirectory(dir="/run") as folder:
        parent = Path(folder)
        file = parent / "legacy.db"
        db = sqlite3.connect(file)
        try:
            assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            db.execute("CREATE TABLE sample(v INTEGER)")
            db.execute("INSERT INTO sample VALUES(7)")
            db.commit()
        finally:
            db.close()
        os.chown(parent, user.pw_uid, user.pw_gid)
        os.chown(file, user.pw_uid, user.pw_gid)
        args = ["systemd-run", "--quiet", "--wait", "--pipe", "--collect", "--uid=" + user.pw_name,
                "-p", "ProtectSystem=" + service["ProtectSystem"], "-p", "PrivateTmp=yes"]
        paths = {str(LEGACY.parent): str(parent), str(LEGACY): str(file)}
        for option in ("ReadWritePaths", "ReadOnlyPaths"):
            mounts = [paths[p] for p in service.get(option, "").split() if p in paths]
            if mounts:
                args.extend(["-p", option + "=" + " ".join(mounts)])
        subprocess.run([*args, "/usr/bin/python3", "-B", "-c", READ_TEST, str(file)], check=True)
        print(unit, "cold WAL read OK; primary database mounted read-only")


if __name__ == "__main__":
    for name in ("tt-promotion.service", "tt-promotion-run.service"):
        check(name)
