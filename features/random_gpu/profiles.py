"""Canvas for the frozen random-overlay media profile, independent of execution host."""
HD = (720, 1280)
SD = (480, 854)

SD_PROFILES = {
    "tt-post-random-overlay-h264-720x1280-v3": "tt-post-random-overlay-h264-480x854-v3",
    "tt-post-random-overlay-hevc-720x1280-v3": "tt-post-random-overlay-hevc-480x854-v3",
    "tt-post-random-overlay-libx264-720x1280-v1": "tt-post-random-overlay-libx264-480x854-v1",
    "fb-post-random-overlay-libx264-720x1280-v1": "fb-post-random-overlay-libx264-480x854-v1",
    "x-random-overlay-h264-720x1280-v1": "x-random-overlay-h264-480x854-v1",
    "x-random-overlay-libx264-720x1280-v1": "x-random-overlay-libx264-480x854-v1",
    "fb-random-overlay-ci-apng-h264-v1": "fb-random-overlay-ci-apng-h264-480x854-v2",
    "x-random-overlay-ci-apng-h264-v1": "x-random-overlay-ci-apng-h264-480x854-v2",
}
HD_PROFILES = {new: old for old, new in SD_PROFILES.items()}


def canvas(profile):
    return SD if profile in HD_PROFILES else HD


def base_profile(profile):
    return HD_PROFILES.get(profile, profile)
