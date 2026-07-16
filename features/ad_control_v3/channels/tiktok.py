"""TikTok contract placeholder; every operation fails closed in this release."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from ..errors import AdControlV3Error
from .base import ChannelAdapter


class TikTokAdapter(ChannelAdapter):
    channel = "tiktok"
    enabled = False

    def capabilities(self) -> Dict[str, Any]:
        return {
            "channel": self.channel,
            "enabled": False,
            "object_levels": [],
            "observe": False,
            "live_pause": False,
            "live_copy": False,
            "fields": [],
            "reason": "channel_not_enabled",
        }

    def discover(self, scope: Mapping[str, Any]) -> List[Dict[str, Any]]:
        raise AdControlV3Error(
            "channel_not_enabled",
            "TikTok control is reserved for a later release",
            status=409,
            details={"channel": self.channel},
        )
