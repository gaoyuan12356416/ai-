"""YouTube publication isolation is per task, never per channel or material."""
import re


def task_resource(task_id):
    value = str(task_id or "")
    return "youtube:task:" + value if re.fullmatch(r"[0-9]+", value) else ""


def identity_resources(task_id):
    resource = task_resource(task_id)
    return [resource] if resource else []
