#!/usr/bin/env python3
"""Stage/activate/rollback only the independent daily report's billing reader."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import deploy_social_daily_owner_card as deploy

deploy.BASELINE = "cedb6334cf9d5e9c2c28a96b067629668fb26d52"
deploy.FILES = (
    "features/social_daily_report/billing.py",
    "features/social_daily_report/collector.py",
    "features/social_analytics/routes.py",
)

if __name__ == "__main__":
    deploy.main()
