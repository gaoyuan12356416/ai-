"""Run the existing tag planner, deferring new claims during a dependency drain."""
import sys
from pathlib import Path

sys.path.insert(0, "/opt/x-tag-publisher/current")

from scripts import x_auto_post_runner as runner


DRAIN_UNITS = ("drama-material-api.service", "pipeline-coordinator.service",
               "x-post-automation.service", "x-tag-post.service")


class DrainAwareClient(runner.SidecarClient):
    def post(self, path, payload, timeout):
        if path == "/internal/x-auto-post/execute-next" and any(
            (Path("/run/pipeline-service-drain") / (unit + ".json")).exists()
            for unit in DRAIN_UNITS
        ):
            return {"ok": True, "claimed": False, "deferred": "service_draining"}
        return super().post(path, payload, timeout)


if __name__ == "__main__":
    runner.SidecarClient = DrainAwareClient
    raise SystemExit(runner.main())
