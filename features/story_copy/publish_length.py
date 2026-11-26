"""Budget the complete, rendered AI caption at the publisher boundary."""
import regex
from twitter_text import parse_tweet


def publish_limit(task):
    if task["endpoint"].startswith("x_"):
        return 280, "X weighted characters"
    if task["endpoint"] == "fb":
        return (5000, "UTF-8 bytes") if task["family"] == "fv" else (5000, "Unicode characters")
    raise ValueError("unknown_caption_endpoint")


def fit_caption(task, text, url):
    limit, unit = publish_limit(task)
    if unit == "X weighted characters":
        def measure(value):
            return parse_tweet(value).weightedLength
    elif unit == "UTF-8 bytes":
        def measure(value):
            return len(value.encode("utf-8"))
    else:
        measure = len

    if measure(text) <= limit:
        return text
    if not text.startswith(url + "\n") or measure(url) > limit:
        raise ValueError("caption_link_exceeds_budget")

    # The first line is the intact redirect. Keep whole trailing tags where
    # possible and truncate the prose on a user-perceived character boundary.
    prose, sep, tag_line = text[len(url) + 1:].rpartition("\n\n")
    if not sep or not tag_line.startswith("#"):
        prose, tag_line = text[len(url) + 1:], ""
    tags = tag_line.split()
    while tags and measure(url + "\n…\n\n" + " ".join(tags)) > limit:
        tags.pop()
    suffix = "\n\n" + " ".join(tags) if tags else ""
    if measure(url + "\n…" + suffix) > limit:
        return url
    clusters = regex.findall(r"\X", prose)
    def candidate(n):
        return url + "\n" + "".join(clusters[:n]).rstrip() + "…" + suffix
    low, high = 0, len(clusters)
    while low < high:
        mid = (low + high + 1) // 2
        if measure(candidate(mid)) <= limit:
            low = mid
        else:
            high = mid - 1
    return candidate(low)
