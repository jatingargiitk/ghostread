"""Command line interface for ghostread."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from . import __version__
from .core import scan_html


def load(src: str) -> str:
    if src == "-":
        return sys.stdin.read()
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": f"ghostread/{__version__}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
    with open(src, encoding="utf-8", errors="replace") as f:
        return f.read()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ghostread", description="Show text an AI agent reads on a web page that a human cannot see.")
    ap.add_argument("source", help="URL, HTML file path, or - for stdin")
    ap.add_argument("--json", action="store_true", help="print findings as JSON")
    ap.add_argument("--strip", action="store_true", help="print only the human-visible text (safe to hand to an agent)")
    ap.add_argument("--all", action="store_true", help="also list hidden text that does not look like an instruction")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)
    try:
        html = load(a.source)
    except Exception as e:  # noqa: BLE001
        print(f"ghostread: cannot read {a.source}: {e}", file=sys.stderr)
        return 2
    findings, visible = scan_html(html)
    flagged = [f for f in findings if f.instruction]
    if a.strip:
        print(visible)
    elif a.json:
        print(json.dumps({"source": a.source, "findings": [f.to_dict() for f in findings],
                          "instructions": len(flagged)}, indent=2, ensure_ascii=False))
    else:
        shown = findings if a.all else flagged
        for f in shown:
            mark = "!!" if f.instruction else "  "
            text = f.text if len(f.text) <= 160 else f.text[:157] + "..."
            print(f"{mark} line {f.line:<4} {f.kind:<15} ({f.reason}) {text}")
        print(f"{len(findings)} hidden item(s), {len(flagged)} instruction-shaped"
              + ("" if a.all or not findings else "  (use --all to list every item)"))
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
