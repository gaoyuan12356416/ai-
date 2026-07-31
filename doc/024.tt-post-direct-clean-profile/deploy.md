# TT Post clean Direct Post deployment

## Formal clean configuration

GPU:

```text
TT_POST_GPU_MEDIA_MODE=direct_clean
TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=0
```

CPU:

```text
TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=0
TT_POST_MEDIA_PROFILE_VERSION=tt-post-direct-clean-hevc-720x1280-v1
```

Keep every formal gate closed while deploying and preparing the reviewed
source. Verify GPU health reports:

```text
media_mode=direct_clean
profile=tt-post-direct-clean-hevc-720x1280-v1
brand_overlay_review_required=false
direct_post_eligible=true
transition=none
```

Open the GPU gates first and verify the exact pull origin. Open the CPU gates
only after there is no old due queue and the intended clean pool item is the
sole next item. A manual run-now must use one stable idempotency key; an
unknown init outcome must be reconciled and never retried as a new post.

## Rollback

Close all CPU and GPU formal gates, then restore:

```text
TT_POST_GPU_MEDIA_MODE=branded_preview
TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=4.333333
TT_POST_MEDIA_PROFILE_VERSION=tt-post-hevc-720x1280-v2
```

Do not delete prepared manifests or publish ledgers. Preserve them for
identity checks and reconciliation.

