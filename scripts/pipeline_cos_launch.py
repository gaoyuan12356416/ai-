"""Start one additional preparation bridge or a collector-free HTTP handoff."""
import argparse
import json
import os
import subprocess
from pathlib import Path


def main():
    args = argparse.ArgumentParser()
    args.add_argument("config", type=Path)
    cfg = json.loads(args.parse_args().config.read_text())
    pid = subprocess.check_output(["systemctl","show",cfg["owner"],"-p","MainPID","--value"],text=True).strip()
    process = Path("/proc") / str(int(pid))
    env = dict(part.decode().split("=", 1) for part in (process / "environ").read_bytes().split(b"\0") if b"=" in part)
    env.update(cfg.get("environment", {}))
    os.chdir(cfg["runtime"])
    argv = cfg["argv"]
    os.execve(argv[0], argv, env)


if __name__ == "__main__":
    main()
