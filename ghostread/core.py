"""HTML hidden-text detection using only the standard library."""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}
SKIP_CONTENT = {"script", "style", "noscript", "template"}

ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
TAG_CHARS = re.compile("[\U000E0000-\U000E007F]")
BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")

INSTRUCTION = re.compile(
    r"\b(ignore (all |any )?(previous|prior|above) (instructions|prompts?)"
    r"|disregard (the |all )?(previous|above|prior)"
    r"|you are (now )?(an?|the) (ai|assistant|agent|llm)"
    r"|(as an?|dear) (ai|llm|assistant|language model|agent)"
    r"|system prompt|new instructions|do not (tell|inform|mention to) the user"
    r"|(run|execute) (this|the following) (command|code)"
    r"|send (the|your|all) .{0,40}(to|at) https?://"
    r"|exfiltrate|api[_ ]?key|curl\s+-|rm\s+-rf)",
    re.I,
)

WHITE = {"#fff", "#ffffff", "white", "rgb(255,255,255)", "rgba(255,255,255,1)"}


def _style(s: str) -> dict:
    out = {}
    for part in (s or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().lower()] = re.sub(r"\s+", "", v.strip().lower()).replace("!important", "")
        # ignore malformed declarations
    return out


def _px(v: str):
    m = re.match(r"^(-?\d+(?:\.\d+)?)(px|pt|em|rem)?$", v or "")
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or "px"
    return n * {"px": 1, "pt": 1.333, "em": 16, "rem": 16}[unit]


def hidden_reason(attrs: dict, css_rules: dict) -> str | None:
    """Return why an element is hidden from humans, or None if visible."""
    if "hidden" in attrs:
        return "hidden attribute"
    st = {}
    for cls in (attrs.get("class") or "").split():
        st.update(css_rules.get("." + cls, {}))
    if attrs.get("id"):
        st.update(css_rules.get("#" + attrs["id"], {}))
    st.update(_style(attrs.get("style", "")))
    if st.get("display") == "none":
        return "display:none"
    if st.get("visibility") in ("hidden", "collapse"):
        return "visibility:hidden"
    if st.get("opacity") not in (None, "") and _num(st["opacity"]) is not None and _num(st["opacity"]) < 0.05:
        return "opacity:" + st["opacity"]
    fs = _px(st.get("font-size", ""))
    if fs is not None and fs < 2:
        return "font-size:" + st["font-size"]
    for k in ("left", "top", "text-indent", "margin-left"):
        v = _px(st.get(k, ""))
        if v is not None and v <= -1000:
            return f"{k}:{st[k]}"
    if st.get("clip") in ("rect(0,0,0,0)", "rect(0px,0px,0px,0px)") or st.get("clip-path") in ("inset(50%)", "inset(100%)"):
        return "clipped"
    w, h = _px(st.get("width", "")), _px(st.get("height", ""))
    if w is not None and h is not None and w <= 1 and h <= 1 and st.get("overflow") == "hidden":
        return "1px box"
    color = st.get("color")
    bg = st.get("background-color") or st.get("background")
    if color and color in WHITE and bg and bg in WHITE:
        return "white on white"
    if color == "transparent":
        return "color:transparent"
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_css(text: str) -> dict:
    """Very small CSS reader: simple .class / #id selectors only."""
    rules: dict = {}
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        decl = _style(body)
        for s in sel.split(","):
            s = s.strip()
            if re.fullmatch(r"[.#][\w-]+", s):
                rules.setdefault(s, {}).update(decl)
    return rules


@dataclass
class Finding:
    kind: str       # hidden-element | comment | zero-width | unicode-tags | bidi | attribute
    reason: str
    text: str
    line: int
    instruction: bool

    def to_dict(self):
        return asdict(self)


def decode_tags(s: str) -> str:
    return "".join(chr(ord(c) - 0xE0000) if 0xE0020 <= ord(c) <= 0xE007E else "" for c in s if 0xE0000 <= ord(c) <= 0xE007F)


class _Parser(HTMLParser):
    def __init__(self, css_rules):
        super().__init__(convert_charrefs=True)
        self.css = css_rules
        self.stack = []  # (tag, hidden_reason or None, start_line)
        self.skip = 0
        self.hidden_buf: list = []  # [reason, line, [text]]
        self.findings: list[Finding] = []
        self.visible: list[str] = []
        self.agent_text: list[str] = []

    def _hidden_depth(self):
        for tag, r, line in self.stack:
            if r:
                return r, line
        return None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        for key in ("alt", "title", "aria-label"):
            v = a.get(key, "").strip()
            if v and INSTRUCTION.search(v):
                self._add("attribute", f"{key}= attribute", v, self.getpos()[0])
        if tag in SKIP_CONTENT:
            self.skip += 1
        if tag in VOID:
            return
        r = hidden_reason(a, self.css)
        outer = self._hidden_depth()
        self.stack.append((tag, r, self.getpos()[0]))
        if r and not outer:
            self.hidden_buf = [r, self.getpos()[0], []]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in SKIP_CONTENT and self.skip:
            self.skip -= 1
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                was = self._hidden_depth()
                del self.stack[i:]
                if was and not self._hidden_depth():
                    self._flush()
                return

    def _flush(self):
        if self.hidden_buf:
            r, line, parts = self.hidden_buf
            txt = " ".join(" ".join(parts).split())
            if txt:
                self._add("hidden-element", r, txt, line)
        self.hidden_buf = []

    def handle_comment(self, data):
        t = " ".join(data.split())
        if t:
            self._add("comment", "HTML comment", t, self.getpos()[0])

    def handle_data(self, data):
        if self.skip:
            return
        line = self.getpos()[0]
        if TAG_CHARS.search(data):
            self._add("unicode-tags", "invisible Unicode tag characters", decode_tags(data), line)
        if ZERO_WIDTH.search(data):
            self._add("zero-width", f"{len(ZERO_WIDTH.findall(data))} zero-width chars", " ".join(ZERO_WIDTH.sub("", data).split()), line)
        if BIDI.search(data):
            self._add("bidi", "bidi control characters", " ".join(BIDI.sub("", data).split()), line)
        clean = BIDI.sub("", ZERO_WIDTH.sub("", TAG_CHARS.sub("", data)))
        self.agent_text.append(data)
        if self._hidden_depth():
            self.hidden_buf[2].append(clean)
        else:
            self.visible.append(clean)

    def _add(self, kind, reason, text, line):
        self.findings.append(Finding(kind, reason, text, line, bool(INSTRUCTION.search(text))))

    def close(self):
        super().close()
        if self._hidden_depth():
            self._flush()


def scan_html(html: str):
    """Scan HTML. Returns (findings, visible_text)."""
    css = {}
    for block in re.findall(r"<style[^>]*>(.*?)</style>", html, flags=re.S | re.I):
        css.update(parse_css(block))
    p = _Parser(css)
    p.feed(html)
    p.close()
    visible = re.sub(r"[ \t]+", " ", "".join(p.visible))
    visible = re.sub(r"\n\s*\n+", "\n\n", visible).strip()
    return p.findings, visible
