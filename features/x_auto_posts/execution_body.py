"""The template frozen for the publisher of the selected material."""


def execution_body(template, material):
    return template.replace("{{url}}", "{{m_url}}") if material.get("source_provider") == "moboreels" else template
