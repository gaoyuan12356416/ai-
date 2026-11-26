"""Facebook Page automatic publishing sidecar."""
__all__ = ["FBAutoPostStore", "StoreError"]


def __getattr__(name):
    # Standalone metric readers can import their adapters without loading the publishing service.
    if name in __all__:
        from . import core
        return getattr(core, name)
    raise AttributeError(name)
