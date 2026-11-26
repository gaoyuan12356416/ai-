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

# Fixed localized CTA prefix; a short model-written story hook follows it.
CLICK_PREFIX = {
    "zh": "立即观看完整版",
    "zh-tw": "立即觀看完整版",
    "ja": "今すぐ全編を視聴",
    "ko": "지금 전체 영상 보기",
    "th": "ดูเรื่องเต็มได้เลย",
    "en": "watch full now",
    "es": "Mira la versión completa ahora",
    "pt": "Assista à versão completa agora",
    "pt-pt": "Vê a versão completa agora",
    "fr": "Regardez en entier maintenant",
    "de": "Jetzt komplett ansehen",
    "it": "Guarda subito la versione completa",
    "pl": "Obejrzyj całość już",
    "cs": "Sleduj celé hned",
    "id": "Tonton penuh sekarang",
    "ms": "Tonton versi penuh sekarang",
    "vi": "Xem bản đầy đủ ngay",
    "ar": "شاهد النسخة الكاملة الآن",
    "tr": "Şimdi tamamını izle",
    "ru": "Смотри полную версию прямо сейчас",
    "hi": "अभी पूरा देखें",
    "tl": "Panoorin nang buo ngayon",
    "fil": "Panoorin nang buo ngayon",
    "el": "Δες το ολόκληρο τώρα",
    "ro": "Urmărește acum versiunea completă",
    "uk": "Дивись повну версію зараз",
    "nl": "Bekijk nu de volledige versie",
    "da": "Se hele serien nu",
    "sv": "Se hela nu",
    "hu": "Nézd meg most a teljes történetet",
    "bn": "এখনই সম্পূর্ণ দেখুন",
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


def click_options(task, values):
    prefix = click_prefix(task["language"])
    lang = canonical_lang(task["language"]).split("-")[0]
    script = SCRIPTS.get(lang)
    options = []
    field_error = None
    for index, value in enumerate(values if isinstance(values, list) else []):
        try:
            hook = one_line(value, {"zh": 16, "ja": 24}.get(lang, 66), f"hooks[{index}]")
        except ValueError as exc:
            if field_error is None:
                field_error = str(exc)
            continue
        key = normalized(hook)
        if (len(key) < 2 or len(hook.split()) > 12
                or (script and not re.search(script, hook))
                or normalized(prefix) in key
                or re.search(r"[↑⬆☝👉👆]", hook)
                or (task.get("episode") and re.search(
                    r"直[達达看]|直行|direct(?:ly)? to|directo al episodio|direto ao epis[oó]dio", hook, re.I))):
            continue
        separator = {"zh": "，", "ja": "｜", "ko": " · "}.get(lang, " ")
        if hook[0] in ",،，.!?:—–":
            separator = ""
        cta = "👆 " + prefix + separator + hook
        if key not in {o["hook_key"] for o in options}:
            options.append(dict(click_hook=hook, hook_key=key, cta=cta,
                                cta_style="watch_full_now_hook"))
    if not options:
        if field_error:
            raise ValueError(field_error.replace("invalid_copy_field:", "invalid_click_hook:", 1))
        raise ValueError("invalid_click_hook: hooks: no valid story continuation")
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


def episode_label(lang, title, number):
    label = EPISODES.get(lang, "№ {n}").format(n=number)
    return f"《{title}》{label}" if lang in ("zh", "ja") else f"{title} · {label}"


def body_length_target(task):
    """Pick one reproducible target per task. Only the two prose fields count."""
    lang = canonical_lang(task["language"]).split("-")[0]
    cjk = lang in {"zh", "ja", "ko"}
    if task["endpoint"] == "fb":
        low, high, margin = (55, 100, 10) if cjk else (120, 210, 20)
    else:
        low, high, margin = (38, 60, 8) if cjk else (80, 135, 10)
        # Reserve space for a short link, hook, two tags, icon and separators.
        # The native renderer checks the actual assembled post again.
        fixed = 90 + weight(click_prefix(task["language"]))
        if task["endpoint"] == "x_drama" and task.get("episode"):
            fixed += weight(episode_label(lang, task["title"], task["episode"])) + 3
        room = max(3, (280 - fixed) // (2 if cjk else 1))
        margin = min(margin, (room - 1) // 2)
        high = min(high, room - margin)
        if high < low:
            low = max(margin + 1, high - margin)
    seed = int(signature("body-length-v1:" + task["key"])[:16], 16)
    target = low + seed % (high - low + 1)
    return {"target": target, "min": target - margin, "max": target + margin}


def one_line(value, maximum, field="text"):
    def fail(reason, index=None):
        if index is not None:
            char = value[index]
            reason += f" {char!r} (U+{ord(char):04X}) at {index + 1}"
        raise ValueError(f"invalid_copy_field: {field}: {reason}")

    if not isinstance(value, str):
        fail(f"expected text, got {type(value).__name__}")
    if maximum is not None and len(value) > maximum:
        fail(f"length {len(value)} exceeds {maximum}")
    for index, char in enumerate(value):
        # Degree sign, letterlike units (℃/℉/℥), and CJK square units (㎏/㎞)
        # are ordinary prose, despite sharing Unicode category So with icons.
        unit_symbol = char == "°" or "\u2100" <= char <= "\u214f" or "\u3300" <= char <= "\u33ff"
        category = unicodedata.category(char)
        if (char in "\r\n#\ufe0e\ufe0f" or category == "Cc"
                or (category == "So" and not unit_symbol)):
            fail("forbidden character", index)
    marker = re.search(r"(?:https?://|www\.|\{\{)", value, re.I)
    if marker:
        fail("template marker" if marker.group() == "{{" else "URL marker", marker.start())
    if not value.strip():
        fail("empty text")
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
        k: one_line(item.get(k), limit, k)
        for k, limit in (("opening", None), ("turn", None), ("angle", 90))
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
    output["body_chars"] = body_length_target(task)
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
        episode = episode_label(lang, one_line(task["title"], 500, "title"), int(task["episode"]))
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
    from .publish_length import fit_caption
    return fit_caption(task, text, url)
