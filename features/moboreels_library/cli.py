import argparse
import json
import urllib.request
from .settings import Settings


def main():
    parser = argparse.ArgumentParser(description="Moboreels operations")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync", help="Enqueue one synchronization through the running service")
    commands.add_parser("report-sync", help="Enqueue orders and advertising revenue synchronization")
    resolve = commands.add_parser("resolve", help="Resolve an unknown creation after checking the KOC platform")
    resolve.add_argument("--star-user-id", required=True)
    resolve.add_argument("--story-id", required=True)
    resolve.add_argument("--decision", choices=["created", "not-created"], required=True)
    resolve.add_argument("--evidence", required=True)
    resolve.add_argument("--operator", required=True)
    resolve.add_argument("--koc-code")
    resolve.add_argument("--deep-link")
    resolve.add_argument("--home-link")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command in ("sync", "report-sync"):
        if not settings.internal_token:
            raise SystemExit("Internal service credential is missing")
        body = {"method": "POST", "path": "/sync-runs" if args.command == 'sync' else '/report-sync-runs', "body": {"trigger": "scheduled"},
                "actor": {"tenant_key": "system", "user_id": "moboreels-scheduler", "name": "每日定时同步"}}
        request = urllib.request.Request("http://127.0.0.1:{}/internal/dispatch".format(settings.bind_port),
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + settings.internal_token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=30) as response:
            payload = json.load(response)
        run = payload["run"]
        print(json.dumps({"run_id": run.get("id"), "status": run.get("status"), "reused": payload.get("reused")}))
    else:
        from .db import Database
        from .promotions import PromotionManager
        from .provider import ProviderClient
        manager = PromotionManager(settings, Database(settings), ProviderClient(settings))
        result = manager.resolve(args.star_user_id, args.story_id, args.decision, args.evidence,
            {"koc_code": args.koc_code, "deep_link": args.deep_link, "home_link": args.home_link}, args.operator)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
