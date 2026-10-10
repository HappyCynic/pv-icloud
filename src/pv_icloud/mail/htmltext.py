# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""HTML mail body -> readable plain text, using only the standard library.

Images are dropped and nothing is fetched. Scripts, styles and hidden elements
(`display:none`, `visibility:hidden`, `hidden`: preheaders, mobile duplicates) are dropped too.
Paragraphs, line breaks, lists, table rows, quotes and <pre> keep their shape; links keep
their target as "text (url)" for http(s) and mailto. Tolerant of the malformed HTML mail is
full of: unclosed tags are closed by their parent, stray end tags are ignored.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}  # fmt: skip
_SKIP = {"head", "script", "style", "template", "title", "noscript", "svg", "object", "iframe"}
_BLOCK = {
    "address", "article", "aside", "center", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "header", "li", "main", "nav", "section",
    "tbody", "tfoot", "thead", "tr",
}  # fmt: skip
_PARAGRAPH = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "blockquote", "pre"}
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_WS = re.compile(r"\s+")
# Zero-width and soft-hyphen characters; marketing mail pads preheaders with them.
_INVISIBLE = re.compile("[­͏​-‏⁠﻿]")
_LINK_SCHEMES = ("http://", "https://", "mailto:")


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.line = ""
        self.blank = False  # a blank line goes before the next emitted line
        self.stack: list[tuple[str, bool]] = []  # open elements: (tag, hidden)
        self.lists: list[list[int]] = []  # open lists: [counter], -1 = unordered
        self.links: list[tuple[str | None, int, int]] = []  # (href, lines, chars) at <a>
        self.quote = 0
        self.last_quote = 0  # quote depth of the last emitted line
        self.pre = 0
        self.cells = 0

    # -- output --
    def _emit(self, text: str) -> None:
        if self.blank and self.lines:  # quoted only if both neighbours are
            self.lines.append(("> " * min(self.quote, self.last_quote)).rstrip())
        self.blank = False
        self.last_quote = self.quote
        self.lines.append("> " * self.quote + text)

    def _break(self, blank: bool = False) -> None:
        if self.line.strip():
            self._emit(self.line.rstrip())
        self.line = ""
        self.blank = self.blank or blank

    def _write(self, text: str) -> None:
        if not self.line or self.line.endswith(" "):
            text = text.lstrip()
        self.line += text

    @property
    def hidden(self) -> bool:
        return bool(self.stack) and self.stack[-1][1]

    # -- parser callbacks --
    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        data = _INVISIBLE.sub("", data)
        if self.pre:
            first, *rest = data.split("\n")
            self.line += first
            for part in rest:
                self._emit(self.line.rstrip())
                self.line = part
            return
        self._write(_WS.sub(" ", data))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self.stack.clear()  # closes an unterminated <head>
        if tag in _VOID:
            if self.hidden:
                return
            if tag == "br":
                if self.line.strip():
                    self._break()
                else:
                    self.blank = True
            elif tag == "hr":
                self._break(blank=True)
            return
        a = dict(attrs)
        hidden = (
            self.hidden
            or tag in _SKIP
            or "hidden" in a
            or bool(_HIDDEN_STYLE.search(a.get("style") or ""))
        )
        self.stack.append((tag, hidden))
        if hidden:
            return
        if tag in _PARAGRAPH or tag in _BLOCK:
            self._break(blank=tag in _PARAGRAPH and not (tag in ("ul", "ol") and self.lists))
        if tag in ("ul", "ol"):
            self.lists.append([0 if tag == "ol" else -1])
        elif tag == "li":
            indent = "  " * max(len(self.lists) - 1, 0)
            if self.lists and self.lists[-1][0] >= 0:
                self.lists[-1][0] += 1
                self.line = f"{indent}{self.lists[-1][0]}. "
            else:
                self.line = f"{indent}- "
        elif tag == "blockquote":
            self.quote += 1
        elif tag == "pre":
            self.pre += 1
        elif tag == "tr":
            self.cells = 0
        elif tag in ("td", "th"):
            if self.cells:
                self._write(" | ")
            self.cells += 1
        elif tag == "a":
            href = (a.get("href") or "").strip()
            keep = href if href.lower().startswith(_LINK_SCHEMES) else None
            self.links.append((keep, len(self.lines), len(self.line)))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                break
        else:
            return  # stray end tag
        while len(self.stack) > i:
            name, hidden = self.stack.pop()
            if not hidden:
                self._close(name)

    def _close(self, tag: str) -> None:
        if tag == "a" and self.links:
            href, lines, chars = self.links.pop()
            if href is None:
                return
            same_line = lines == len(self.lines)
            text = self.line[chars:].strip() if same_line else "…"
            target = href[7:] if href.lower().startswith("mailto:") else href
            if text and text not in (href, target):
                self._write(f" ({target})")
            return
        if tag in ("ul", "ol") and self.lists:
            self.lists.pop()
        elif tag == "blockquote":
            self._break(blank=True)
            self.quote = max(self.quote - 1, 0)
        elif tag == "pre":
            self._break(blank=True)
            self.pre = max(self.pre - 1, 0)
        if tag in _PARAGRAPH:
            self._break(blank=not (tag in ("ul", "ol") and self.lists))
        elif tag in _BLOCK:
            self._break()

    def text(self) -> str:
        self.close()
        while self.stack:  # run the closing actions of unterminated elements
            name, hidden = self.stack.pop()
            if not hidden:
                self._close(name)
        self._break()
        return "\n".join(self.lines).strip()


def html_to_text(html: str) -> str:
    try:
        converter = _Converter()
        converter.feed(html)
        return converter.text()
    except Exception:  # never let a hostile body break parsing; fall back to tag stripping
        return _WS.sub(" ", re.sub(r"<[^>]+>", " ", html)).strip()
