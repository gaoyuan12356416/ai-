"""Deploy Video credential selection from GitHub; roll back code, never ledgers."""
import deploy_meta_asset_recovery as rollout


if __name__ == "__main__":
    rollout.BASE = "1b86d2afc3859ab4d70a0693226fe0873ebbac79"
    rollout.FILES = ["features/fb_ad_asset_delete/" + name + ".py"
                     for name in ("bridge", "graph", "service", "source", "store")]
    rollout.FILES += ["static/fb-post-ad-delete." + ext for ext in ("html", "js", "css")]
    rollout.main()
