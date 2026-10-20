"""Insert only the private identity route into the verified current main API."""
from __future__ import annotations

import argparse
import ast
from pathlib import Path

MARKER = "# FB_PAGE_MATERIAL_POOL_IDENTITY_V1"
PATH = "/internal/fb-video-publish/identity"


def patch_app(text):
    tree = ast.parse(text)
    handlers = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DramaMaterialHandler"]
    if len(handlers) != 1:
        raise ValueError("Main handler anchor changed")
    methods = [node for node in handlers[0].body if isinstance(node, ast.FunctionDef) and node.name == "do_POST"]
    if len(methods) != 1:
        raise ValueError("POST handler anchor changed")
    method = methods[0]
    # An existing marker must still represent the exact dispatcher, not drift.
    insertion = (
        "        " + MARKER + "\n"
        '        if parsed.path == "' + PATH + '":\n'
        "            from features.fb_video_publish.identity_broker import dispatch\n"
        "            return dispatch(self, globals())\n"
    )
    if MARKER in text:
        if text.count(MARKER) != 1 or insertion not in ast.get_source_segment(text, method).replace("\r\n", "\n"):
            raise ValueError("Existing identity integration drift")
        return text
    if PATH in text:
        raise ValueError("Unrecognized identity route")
    if "FB_VIDEO_PUBLISH_INTEGRATION_V1" not in text:
        raise ValueError("Install the existing video publishing integration first")
    assignments = [node for node in method.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "parsed" for target in node.targets)]
    if len(assignments) != 1:
        raise ValueError("Parsed request anchor changed")
    node = assignments[0]
    if not isinstance(node.value, ast.Call):
        raise ValueError("Parsed request anchor changed")
    lines = text.splitlines(keepends=True)
    # Preserve every unrelated byte, including the live file's blank-line layout.
    newline = "\r\n" if "\r\n" in text else "\n"
    lines.insert(node.end_lineno, insertion.replace("\n", newline))
    result = "".join(lines)
    ast.parse(result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        raise ValueError("Stage to a distinct output path")
    source = args.source.read_bytes().decode("utf-8")
    args.output.write_bytes(patch_app(source).encode("utf-8"))
    print("Staged one private identity route; all unrelated source preserved")


if __name__ == "__main__":
    main()
