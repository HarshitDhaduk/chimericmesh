"""PreToolUse guard: block HTML-injection sinks in the dashboard sources.

Reads the hook JSON from stdin. Exits 2 (block) only when a write targets
src/chimericmesh/web/ and the text uses innerHTML/outerHTML/insertAdjacentHTML/
document.write. Every other case, including bad input, exits 0 silently.
"""
import json
import re
import sys

SINK = re.compile(
    r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML\s*\(|document\.write\s*\("
)


def main() -> int:
    try:
        data = json.loads(sys.stdin.read())
        tool_input = data.get("tool_input") or {}
        path = str(tool_input.get("path") or "").replace("\\", "/")
        text = tool_input.get("text")
        if text is None:
            text = tool_input.get("newStr")
        text = text if isinstance(text, str) else ""
        if "src/chimericmesh/web/" in path and SINK.search(text):
            sys.stderr.write(
                "Blocked: innerHTML/outerHTML/insertAdjacentHTML/document.write "
                "in the dashboard. Log text is untrusted; render it with "
                "textContent instead.\n"
            )
            return 2
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
