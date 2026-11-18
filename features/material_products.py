"""Material product filters shared by automatic publishing templates."""

DEFAULT = "Dramawave"
KNOWN = {p.casefold(): p for p in (DEFAULT, "Moboreels", "Moboshort", "DramawaveSource")}


def normalize(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 30:
        raise ValueError("至少选择一个素材产品")
    result, seen = [], set()
    for value in values:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 128 or any(ord(c) < 32 for c in value):
            raise ValueError("素材产品名称无效")
        value = value.strip()
        key = value.casefold()
        if key not in seen:
            result.append(KNOWN.get(key, value))
            seen.add(key)
    return tuple(result)


def selected(config):
    if "material_products" in config:
        return normalize(config["material_products"])
    return normalize([config.get("material_product") or config.get("product") or DEFAULT])


def contains(config, product):
    return str(product or "").strip().casefold() in {p.casefold() for p in selected(config)}
