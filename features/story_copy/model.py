"""Content rules shared by preparation and the final platform renderer."""

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit

ENDPOINTS = ("fb", "x_material", "x_drama", "x_tag_auto")
ICONS = {
    "intrigue": "♟︎",
    "romance": "♡",
    "magic": "✦",
    "mystery": "◇",
    "fantasy": "☾",
    "conflict": "✧",
}
ANGLES = (
    "人物选择",
    "重要物件",
    "身份悬念",
    "关系变化",
    "选择的代价",
    "观众视角",
    "人物动机",
    "关键冲突",
)
EPISODES = {
    "zh": "第 {n} 集",
    "ja": "第{n}話",
    "ko": "{n}화",
    "en": "Episode {n}",
    "es": "Episodio {n}",
    "pt": "Episódio {n}",
    "fr": "Épisode {n}",
    "de": "Folge {n}",
    "id": "Episode {n}",
    "pl": "Odcinek {n}",
    "cs": "Díl {n}",
    "th": "ตอนที่ {n}",
    "vi": "Tập {n}",
    "ar": "الحلقة {n}",
    "tr": "Bölüm {n}",
    "ru": "Серия {n}",
    "it": "Episodio {n}",
    "ms": "Episod {n}",
    "hi": "एपिसोड {n}",
    "el": "Επεισόδιο {n}",
    "ro": "Episodul {n}",
    "tl": "Episode {n}",
    "fil": "Episode {n}",
}

# Only the tail is model-written. Keep the delimiter in the prefix so CJK
# and Thai do not acquire a space between the prompt and its continuation.
CLICK_PREFIX = {
    "zh": "点链接",
    "zh-tw": "點連結",
    "ja": "リンクをタップして",
    "ko": "링크를 눌러 ",
    "th": "แตะลิงก์",
    "en": "Tap the link ",
    "es": "Toca el enlace ",
    "pt": "Toque no link ",
    "pt-pt": "Toca na ligação ",
    "fr": "Cliquez sur le lien ",
    "de": "Tippe auf den Link ",
    "it": "Tocca il link ",
    "pl": "Kliknij link",
    "cs": "Klikni na odkaz",
    "id": "Ketuk tautan ",
    "ms": "Tekan pautan ",
    "vi": "Nhấn vào liên kết ",
    "ar": "اضغط على الرابط ",
    "tr": "Bağlantıya dokun",
    "ru": "Нажми на ссылку",
    "hi": "लिंक पर टैप करें",
    "tl": "Pindutin ang link ",
    "fil": "Pindutin ang link ",
    "el": "Πάτησε τον σύνδεσμο ",
    "ro": "Apasă pe link ",
    "uk": "Натисни на посилання",
    "nl": "Tik op de link ",
    "sv": "Tryck på länken ",
    "hu": "Kattints a linkre",
    "bn": "লিঙ্কে ট্যাপ করুন",
}
SCRIPTS = {
    "zh": r"[\u3400-\u9fff]",
    "ja": r"[\u3040-\u30ff]",
    "ko": r"[\uac00-\ud7af]",
    "th": r"[\u0e00-\u0e7f]",
    "ar": r"[\u0600-\u06ff]",
    "ru": r"[\u0400-\u04ff]",
    "uk": r"[\u0400-\u04ff]",
    "hi": r"[\u0900-\u097f]",
    "el": r"[\u0370-\u03ff]",
    "bn": r"[\u0980-\u09ff]",
}


def canonical_lang(value):
    lang = str(value or "").strip().lower().replace("_", "-")
    return {
        "jp": "ja",
        "kr": "ko",
        "zh-hant": "zh-tw",
        "zh-hans": "zh-cn",
        "in": "id",
    }.get(lang, lang)


def product(value):
    raw = str(value or "").strip().lower().replace(" ", "")
    return {"1479": "dramawave", "dramawavesource": "dramawave"}.get(raw, raw)


def group_key(task):
    return (
        product(task["product"]),
        str(task["content"]),
        canonical_lang(task["language"]),
    )


def normalized(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(c for c in value if unicodedata.category(c)[0] in "LNM")


def signature(value):
    return hashlib.sha256(value.encode()).hexdigest()


def click_prefix(language):
    lang = canonical_lang(language)
    parts = lang.split("-")
    if parts[0] == "zh":
        lang = "zh-tw" if set(parts) & {"tw", "hk", "mo", "hant"} else "zh"
    prefix = CLICK_PREFIX.get(lang, CLICK_PREFIX.get(parts[0]))
    if not prefix:
        raise ValueError("unsupported_click_language")
    return prefix


GENERIC_HOOKS = frozenset(
    normalized(s)
    for s in (
        "看後續",
        "看后续",
        "看下去",
        "看精彩後續",
        "看精彩后续",
        "看她的故事",
        "看他的故事",
        "to watch now",
        "to keep watching",
        "to see more",
        "to see what happens",
        "to follow their story",
        "para ver más",
        "para ver qué pasa",
        "para ver mais",
        "pour voir la suite",
        "für mehr",
        "続きを見よう",
        "続きを見る",
        "확인해 보세요",
        "untuk menonton",
        "para mapanood",
    )
)


def click_options(task, values):
    prefix = click_prefix(task["language"])
    lang = canonical_lang(task["language"]).split("-")[0]
    script = SCRIPTS.get(lang)
    options = []
    for value in values if isinstance(values, list) else []:
        try:
            hook = one_line(value, {"zh": 16, "ja": 24}.get(lang, 66))
        except ValueError:
            continue
        key = normalized(hook)
        if (
            len(key) < 2
            or key in GENERIC_HOOKS
            or len(hook.split()) > 12
            or (script and not re.search(script, hook))
            or normalized(prefix) in key
            or re.search(r"[↑⬆☝👉👆]", hook)
            or (
                task.get("episode")
                and re.search(
                    r"直[達达看]|直行|direct(?:ly)? to|directo al episodio|direto ao epis[oó]dio",
                    hook,
                    re.I,
                )
            )
        ):
            continue
        # Slavic and Turkish subordinate clauses often start with a comma.
        separator = "" if lang in {"zh", "ja", "th"} or hook[0] in ",،，.!?:—–" else " "
        cta = "👆 " + prefix.rstrip() + separator + hook
        if key not in {o["hook_key"] for o in options}:
            options.append(dict(click_hook=hook, hook_key=key, cta=cta))
    if not options:
        raise ValueError("invalid_click_hook")
    if task.get("endpoint", "").startswith("x_"):
        options.sort(key=lambda item: weight(item["cta"]))
    return options


def weight(text):
    return sum(
        1
        if n <= 0x10FF
        or 0x2000 <= n <= 0x200D
        or 0x2010 <= n <= 0x201F
        or 0x2032 <= n <= 0x2037
        else 2
        for n in map(ord, text)
    )


def one_line(value, maximum):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(c in value for c in "\r\n#")
        or re.search(r"(?:https?://|www\.|\{\{)", value, re.I)
    ):
        raise ValueError("invalid_copy_field")
    if any(
        unicodedata.category(c) in {"Cc", "So"} or c in "\ufe0e\ufe0f" for c in value
    ):
        raise ValueError("invalid_copy_field")
    return value.strip()


def validate(task, item, options=None):
    if item.get("id") != task["key"] or canonical_lang(
        item.get("language")
    ) != canonical_lang(task["language"]):
        raise ValueError("task_language_mismatch")
    refs = item.get("refs")
    facts = task["facts"]
    if (
        not isinstance(refs, list)
        or not refs
        or any(not isinstance(k, str) or k not in facts for k in refs)
    ):
        raise ValueError("unverified_fact")
    if not facts or (task["endpoint"] == "x_drama" and not task.get("episode")):
        raise ValueError("insufficient_evidence")
    if task.get("episode") and "episode" not in facts and item.get("scope") != "series":
        raise ValueError("episode_evidence_missing")
    if item.get("scope") == "episode" and "episode" not in refs:
        raise ValueError("episode_evidence_missing")
    tags = item.get("tags")
    if not isinstance(tags, list) or not 2 <= len(tags) <= 3:
        raise ValueError("invalid_tags")
    clean = [str(tag).strip().lstrip("#") for tag in tags]
    if len(set(clean)) != len(clean) or any(
        not t
        or len(t) > 64
        or any(c != "_" and unicodedata.category(c)[0] not in "LNM" for c in t)
        for t in clean
    ):
        raise ValueError("invalid_tags")
    output = {
        k: one_line(item.get(k), limit)
        for k, limit in (("opening", 640), ("turn", 440), ("angle", 90))
    }
    if not normalized(output["opening"]) or not normalized(output["turn"]):
        raise ValueError("empty_plot")
    script = SCRIPTS.get(canonical_lang(task["language"]).split("-")[0])
    if script and not re.search(script, output["opening"] + output["turn"]):
        raise ValueError("task_language_mismatch")
    output.update((options or click_options(task, item.get("hooks")))[0])
    if task.get("episode") and "episode" not in facts:
        if re.search(
            r"\b(?:in this episode|in episode \d+)\b|本[集话話]|這一集|这一集|今回の|이번\s*화",
            output["opening"] + " " + output["turn"],
            re.I,
        ):
            raise ValueError("episode_evidence_missing")
    output.update(tags=clean, refs=refs, icon=ICONS.get(str(item.get("motif")), "✦"))
    return output


def render(task, draft, url):
    parts = urlsplit(str(url or ""))
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or any(c.isspace() for c in url)
    ):
        raise ValueError("short_link_unavailable")
    cta = draft["cta"]
    if task["endpoint"] == "x_drama":
        lang = canonical_lang(task["language"]).split("-")[0]
        label = EPISODES.get(lang, "№ {n}").format(n=int(task["episode"]))
        title = one_line(task["title"], 500)
        episode = (
            f"《{title}》{label}" if lang in ("zh", "ja") else f"{title} · {label}"
        )
        sep = "｜" if lang in ("zh", "ja") else " · "
        cta = sep.join((cta, episode) if draft.get("click_hook") else (episode, cta))
    text = "\n".join(
        (
            url,
            cta,
            "",
            draft["opening"],
            draft["icon"] + " " + draft["turn"],
            "",
            " ".join("#" + t for t in draft["tags"]),
        )
    )
    if task["endpoint"].startswith("x_") and weight(text) > 280:
        raise ValueError("x_copy_too_long")
    return text
