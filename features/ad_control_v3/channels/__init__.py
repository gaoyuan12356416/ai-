"""Channel adapters for the isolated V3 rule-control service."""

from .base import ChannelAdapter
from .facebook import FacebookAdapter
from .tiktok import TikTokAdapter

__all__ = ["ChannelAdapter", "FacebookAdapter", "TikTokAdapter"]
