"""Replay narrowly anchored changes onto the independently versioned CPU publisher releases."""


def replace(text, old, new, count=1):
    if text.count(old) != count:
        raise ValueError("unsupported current source near " + old.strip()[:100])
    return text.replace(old, new)


def after(line, statements, indent=None):
    prefix = " " * (indent if indent is not None else len(line) - len(line.lstrip()))
    return line + "\n" + "\n".join(prefix + s for s in statements.splitlines())


def optional(statements):
    return (
        "try:\n"
        + "\n".join("    " + s for s in statements.splitlines())
        + "\nexcept Exception as story_error:\n    __import__('logging').getLogger(__name__).warning('story_copy.fallback %s',type(story_error).__name__)"
    )


def fb_auto(text):
    if (
        "from features.story_copy.bridge import fb_plan" in text
        and "fb_final(conn,'fa',row" in text
    ):
        return text
    save = '                conn.execute("UPDATE fb_auto_task SET selection_json=? WHERE id=?",'
    code = optional(
        "from features.story_copy.bridge import fb_plan\nif status == 'planned' and material is not None:\n    fb_plan(selection,self,config,page,material,int(task_cursor.lastrowid),link_timestamp)"
    )
    text = replace(
        text,
        save,
        "\n".join("                " + line for line in code.splitlines())
        + "\n"
        + save,
    )
    mark = """            conn.execute("UPDATE fb_auto_run SET status='running' WHERE id=?", (int(row["run_id"]),))"""
    code = optional(
        "from features.story_copy.bridge import fb_final, fb_redirect\nfb_final(conn,'fa',row,extra_link_writer=fb_redirect)"
    )
    return replace(text, mark, after(mark, code))


def fb_video(text):
    if 'snapshot["story_copy_manual"]' in text and "fb_final(conn,'fv',row)" in text:
        return text
    mark = '                snapshot.update(title=title, manual_policy="operator-v2")'
    text = replace(
        text,
        mark,
        after(
            mark,
            'snapshot["story_copy_manual"] = bool(str(pair.get("caption_template") or "").strip())',
        ),
    )
    find = '            conn.commit()\n            result = dict(conn.execute("SELECT * FROM fb_video_task WHERE task_id=?", (row["task_id"],)).fetchone())\n            result["claim_token"], result["worker_id"] = lease, worker_id'
    code = optional(
        "from features.story_copy.bridge import fb_final\nif kind == 'execute':\n    fb_final(conn,'fv',row)"
    )
    return replace(
        text,
        find,
        "\n".join("            " + l for l in code.splitlines()) + "\n" + find,
    )


def x_service(text):
    if "post_text=x_final(conn,row,post_text" in text:
        return text
    sig = "    def prepare_log(self, log_id, long_url, short_url, post_text):"
    text = replace(
        text, sig, sig.replace("post_text):", "post_text, *, story_product_url=None):")
    )
    mark = '            else:\n                conn.execute(\n                    "UPDATE x_post_publish_log SET long_url=?,short_url=?,post_text=?,updated_at=? WHERE id=?",'
    code = optional(
        "from features.story_copy.bridge import x_final\npost_text=x_final(conn,row,post_text,short_url,self.db_path,story_product_url)"
    )
    text = replace(
        text,
        mark,
        mark.replace(
            "                conn.execute(",
            "\n".join("                " + l for l in code.splitlines())
            + "\n                conn.execute(",
        ),
    )
    old = 'log = store.prepare_log(log["id"], long_url, short_url, post_text)'
    lines = text.splitlines(keepends=True)
    changed = 0
    for i, l in enumerate(lines):
        if old not in l:
            continue
        changed += 1
        if "m_url = resolve_x_url(" in text:
            l = l.replace(old, old[:-1] + ", story_product_url=m_url)")
        lines[i] = after(l.rstrip("\n"), 'post_text = log["post_text"]') + "\n"
    if not changed:
        raise ValueError("classic X final renderer call not found")
    return "".join(lines)


def x_native(text):
    if "short, body, story_product_url=m_url)" in text:
        return text
    old = 'self.store.prepare_log(log["id"], long_url, short, body)'
    if old not in text:
        raise ValueError("native X final renderer call not found")
    if "m_url = resolve_x_url(" in text:
        return replace(text, old, old[:-1] + ", story_product_url=m_url)")
    return text


def fb_supplier_link(text):
    """Finish the deferred decision in the supplier link commit, before Graph writes."""
    marker = 'fb_final(conn, "fa", linked)'
    if marker in text:
        return text
    anchor = '        conn.commit()\n        return dict(conn.execute("SELECT * FROM fb_auto_task'
    hook = (
        '        if row["status"] == "running":\n'
        '            from features.story_copy.bridge import fb_final\n'
        '            linked = conn.execute("SELECT * FROM fb_auto_task WHERE id=?", (current["id"],)).fetchone()\n'
        '            ' + marker + '\n'
    )
    return replace(text, anchor, hook + anchor)


def fb_redirect(text):
    if "from features.story_copy.bridge import restore_fb_optional_link" in text:
        return text
    candidates = [
        (
            'self.short_link_writer(self.short_link_root, int(task["id"]), long_url)',
            "self.store",
        ),
        (
            'self.runtime.executor.short_link_writer(self.runtime.executor.short_link_root, int(task["id"]), long)',
            "self.runtime.store",
        ),
    ]
    for call, owner in candidates:
        if call not in text:
            continue
        line = next(l for l in text.splitlines() if call in l)
        prefix = line[: len(line) - len(line.lstrip())]
        code = (
            "try:\n    "
            + call
            + "\nexcept Exception:\n    from features.story_copy.bridge import restore_fb_optional_link\n"
            "    if not restore_fb_optional_link(" + owner + ",task):\n        raise"
        )
        return replace(text, line, "\n".join(prefix + l for l in code.splitlines()))
    raise ValueError("FB redirect writer not found")


def html(text, endpoint):
    script = (
        '<script defer src="/story-copy-settings.js?v=1.1" data-endpoint="'
        + endpoint
        + '"></script>'
    )
    if script in text:
        return text
    return replace(text, "</body>", script + "\n</body>")


PAGES = {
    "fb-auto-publish-templates.html": "fb",
    "fb-video-publish.html": "fb",
    "fb-pages.html": "fb",
    "fb-post-material-pool.html": "fb",
    "x-post-material-pool.html": "x_material",
    "x-tag-material-pool.html": "x_material",
    "x-post-drama-pool.html": "x_drama",
    "x-tag-auto-publish-templates.html": "x_tag_auto",
}
