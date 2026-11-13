from dataclasses import dataclass
import os
from pathlib import Path


def read_env_file(path):
    result = {}
    if not Path(path).exists():
        return result
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


@dataclass
class Settings:
    data_dir: Path
    institution_user_id: str
    token: str
    internal_token: str
    db_user: str
    db_password: str
    db_host: str = "101.32.56.53"
    read_port: int = 63350
    write_port: int = 63353
    database: str = "ads_ai"
    mysql_binary: str = "/usr/bin/mysql"
    api_base: str = "https://kocapi-cn.cdreader.com"
    api_interval: float = 0.4
    bind_host: str = "127.0.0.1"
    bind_port: int = 8864
    job_db_path: str = "/root/drama_material_service/data/drama_material_jobs.sqlite3"
    macro_token: str = ""

    @classmethod
    def from_env(cls):
        env = read_env_file(os.environ.get("MOBOREELS_BACKEND_ENV", "/root/drama_material_service/.env"))
        env.update(read_env_file(os.environ.get("MOBOREELS_ENV_FILE", "/etc/moboreels-library.env")))
        env.update(read_env_file(os.environ.get("MOBOREELS_MACRO_ENV_FILE", "/etc/moboreels-macro.env")))
        env.update(os.environ)
        return cls(
            data_dir=Path(env.get("MOBOREELS_DATA_DIR", "/mnt/data-disk/moboreels-library")),
            institution_user_id=env.get("MOBOREELS_INSTITUTION_USER_ID", ""),
            token=env.get("MOBOREELS_TOKEN", ""),
            internal_token=env.get("MOBOREELS_INTERNAL_TOKEN", ""),
            db_user=env.get("MOBOREELS_DB_USER", env.get("ADMIN_MAPPING_MYSQL_USER", "")),
            db_password=env.get("MOBOREELS_DB_PASSWORD", env.get("ADMIN_MAPPING_MYSQL_PASSWORD", "")),
            mysql_binary=env.get("MOBOREELS_MYSQL_BINARY", "/usr/bin/mysql"),
            bind_port=int(env.get("MOBOREELS_PORT", "8864")),
            job_db_path=env.get("DRAMA_JOB_DB_PATH", "/root/drama_material_service/data/drama_material_jobs.sqlite3"),
            macro_token=env.get("MOBOREELS_MACRO_TOKEN", ""),
        )
