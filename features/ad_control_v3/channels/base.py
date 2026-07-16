"""Abstract channel boundary. No V2 or application imports are allowed here."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Mapping


class ChannelAdapter(ABC):
    channel = ""
    enabled = False

    @abstractmethod
    def capabilities(self) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def discover(self, scope: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Return deterministic, read-only candidates for an observe preview."""
        raise NotImplementedError

    def pause(self, target: Mapping[str, Any]) -> Dict[str, Any]:
        """External mutation is disabled until a separately approved adapter exists."""
        from ..errors import AdControlV3Error

        raise AdControlV3Error(
            "live_pause_disabled",
            "V3 live pause is disabled",
            status=409,
        )
    def copy(self, target: Mapping[str, Any]) -> Dict[str, Any]:
        """Copy must fail before token lookup or any external request."""
        from ..errors import AdControlV3Error

        raise AdControlV3Error(
            "copy_persistence_not_configured",
            "copy persistence contract is not configured",
            status=409,
        )
