"""Deploy Video credential selection from GitHub; roll back code, never ledgers."""
import deploy_meta_asset_recovery as rollout


if __name__ == "__main__":
    rollout.BASE = "1b86d2afc3859ab4d70a0693226fe0873ebbac79"
    # The d697e30 retirement rollout updated only this live HTML's shared-nav
    # version. Preserve that reviewed generated asset and require its exact hash.
    rollout.BASELINE_HASHES = {"static/fb-post-ad-delete.html":
        "7ad4ce404ec8c9f9e06afaee61ac47c2e4480c729dd9a3bd6f1322315295c088"}
    rollout.FILES = ["features/fb_ad_asset_delete/" + name + ".py"
                     for name in ("bridge", "graph", "service", "source", "store")]
    rollout.FILES += ["static/fb-post-ad-delete." + ext for ext in ("html", "js", "css")]
    rollout.main()
