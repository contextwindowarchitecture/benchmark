"""The HTML representation of a document and the producers that read it (domain-5-plan.md, 6.4).

A DQ document can reach an application as a web page. `page` writes one: a head with the title and a description, a
body with the title as a heading and one paragraph per sentence. Two producers turn pages into evidence bodies, as an
application's retriever would:

- `html` passes the page through as it is;
- `text` extracts the visible text: the heading and the paragraphs, one per line, leaving out the head, comments,
  scripts and styles, images, and any element styled `display:none` or `visibility:hidden`, with everything inside it.

An attack in a comment, the head, a hidden element or an image's alt text (attacks/html.py) reaches the payload under
`html` and is dropped by `text`; the attack-in-payload oracle counts that as dropped by the producer, not resisted.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

PRODUCERS = ("html", "text")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_HIDDEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_VOID = {"img", "meta", "link", "br", "hr", "input"}


def page(title: str, text: str) -> str:
    paragraphs = "\n".join(f"<p>{html.escape(s, quote=False)}</p>" for s in _SENTENCE.split(text) if s)
    return (f'<!DOCTYPE html>\n<html>\n<head>\n<title>{html.escape(title, quote=False)}</title>\n'
            f'<meta name="description" content="{html.escape(title)}">\n</head>\n<body>\n'
            f'<h1>{html.escape(title, quote=False)}</h1>\n{paragraphs}\n</body>\n</html>')


class _Visible(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.current: list[str] = []
        self.skip: list[str] = []  # the open elements whose content is not visible

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        style = dict(attrs).get("style") or ""
        if self.skip or tag in ("head", "script", "style", "title") or _HIDDEN.search(style):
            self.skip.append(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if self.skip[-1] == tag:
                self.skip.pop()
            return
        if tag in ("p", "h1", "h2", "h3", "li", "div", "body"):
            self._flush()

    def handle_data(self, data):
        if not self.skip:
            self.current.append(data)

    def _flush(self):
        line = " ".join("".join(self.current).split())
        if line:
            self.lines.append(line)
        self.current = []


def visible_text(source: str) -> str:
    parser = _Visible()
    parser.feed(source)
    parser.close()
    parser._flush()
    return "\n".join(parser.lines)


def produce(producer: str, source: str) -> str:
    if producer == "html":
        return source
    if producer == "text":
        return visible_text(source)
    raise ValueError(f"unknown producer {producer!r}")
