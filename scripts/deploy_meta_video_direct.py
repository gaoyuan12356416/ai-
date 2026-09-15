"""Deploy direct Video deletion from an exact GitHub checkout, code-only rollback."""
import deploy_meta_asset_recovery as rollout


if __name__ == "__main__":
    rollout.BASE = "00c8872380102e8ca7423f80cea48806a2d71295"
    rollout.FILES = ["features/fb_ad_asset_delete/" + name + ".py"
                     for name in ("core", "graph", "service", "source", "store")]
    rollout.FILES += ["static/fb-post-ad-delete." + ext for ext in ("html", "js", "css")]
    rollout.main()
