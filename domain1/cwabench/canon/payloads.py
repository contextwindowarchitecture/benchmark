"""Strict parsers for the published renderers' payloads (conformance/README.md, Tokenizers and renderers).

A parser recovers each rendered occurrence (stream, tag, id, attributes, rendered body, unescaped body) and the
texts the renderer's token count covers. It accepts only the exact form the renderer writes: anything else is a
ParseError, which is itself a finding.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import jcs

TAG = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
_ATTR = re.compile(r' ([a-z]+)="([^"]*)"')
_BODY_ENTITY = re.compile(r"&(amp|lt|gt);")
_ATTR_ENTITY = re.compile(r"&(amp|lt|gt|quot);")
_ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"'}

RENDERERS = ("fixture-xml/v1", "cwa-messages/v1", "cwa-message-blocks/v1")


class ParseError(ValueError):
    pass


def escape_body(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attribute(text: str) -> str:
    return escape_body(text).replace('"', "&quot;")


def _unescape(text: str, entity: re.Pattern, where: str) -> str:
    """Undo the renderer's escaping, refusing any &, < or > the renderer would have escaped."""
    stray = entity.sub("", text)
    if "&" in stray or "<" in stray or ">" in stray:
        raise ParseError(f"unescaped markup in {where}")
    return entity.sub(lambda m: _ENTITIES[m.group(1)], text)


@dataclass(frozen=True)
class Occurrence:
    stream: str  # "system", "tools" or "xml"
    tag: str | None  # the xml: wrap's tag; None for system and tools
    id: str
    rendered: str  # the body as it appears in the payload: escaped in xml, raw in system and tools
    body: str  # the body unescaped
    conflict: str | None
    speaker: str | None
    attributes: tuple[str, ...]  # attribute names in the order written


@dataclass(frozen=True)
class Parsed:
    renderer: str
    occurrences: list[Occurrence]
    counted: list[str]  # the texts result.input_tokens is the sum of the tokenizer's counts over


def _xml_stream(text: str, allow_speaker: bool) -> list[Occurrence]:
    out: list[Occurrence] = []
    at = 0
    while at < len(text):
        if text[at] != "<":
            raise ParseError(f"expected '<' at offset {at}")
        tag_match = TAG.match(text, at + 1)
        if not tag_match:
            raise ParseError(f"no valid tag at offset {at}")
        tag = tag_match.group(0)
        at = tag_match.end()
        attributes: list[tuple[str, str]] = []
        while True:
            attribute = _ATTR.match(text, at)
            if not attribute:
                break
            attributes.append((attribute.group(1), _unescape(attribute.group(2), _ATTR_ENTITY, "an attribute")))
            at = attribute.end()
        if not text.startswith(">\n", at):
            raise ParseError(f"malformed start tag <{tag}> at offset {at}")
        at += 2
        names = tuple(name for name, _ in attributes)
        allowed = [("id", "speaker", "conflict"), ("id", "speaker"), ("id", "conflict"), ("id",)]
        if names not in allowed or ("speaker" in names and not allow_speaker):
            raise ParseError(f"<{tag}> has attributes {names}")
        values = dict(attributes)
        if "speaker" in values and values["speaker"] not in ("user", "assistant"):
            raise ParseError(f"speaker {values['speaker']!r}")
        close = f"\n</{tag}>\n"
        end = text.find("\n</", at)
        if end < 0 or not text.startswith(close, end):
            raise ParseError(f"<{tag} id={values['id']!r}> is not closed by </{tag}>")
        rendered = text[at:end]
        out.append(Occurrence("xml", tag, values["id"], rendered, _unescape(rendered, _BODY_ENTITY, "a body"),
                              values.get("conflict"), values.get("speaker"), names))
        at = end + len(close)
    return out


def _entry(entry, stream: str) -> Occurrence:
    if not isinstance(entry, dict) or set(entry) not in ({"id", "text"}, {"id", "text", "conflict"}):
        raise ParseError(f"a {stream} entry is not {{id, text[, conflict]}}")
    if not isinstance(entry["id"], str) or not isinstance(entry["text"], str):
        raise ParseError(f"a {stream} entry's id or text is not a string")
    text, conflict = entry["text"], entry.get("conflict")
    if conflict is None:
        body = text
    else:
        if not isinstance(conflict, str):
            raise ParseError("conflict is not a string")
        opening, closing = f'<conflict group="{escape_attribute(conflict)}">\n', "\n</conflict>"
        if not (text.startswith(opening) and text.endswith(closing) and len(text) >= len(opening) + len(closing)):
            raise ParseError(f"{stream} entry {entry['id']!r} is marked {conflict!r} but its text has no conflict mark")
        body = text[len(opening):-len(closing)]
    return Occurrence(stream, None, entry["id"], body, body, conflict, None, ())


def _messages(payload: bytes, blocks: bool) -> Parsed:
    renderer = "cwa-message-blocks/v1" if blocks else "cwa-messages/v1"
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ParseError(f"not JSON: {error}") from None
    if not isinstance(document, dict) or set(document) != {"system", "tools", "messages"}:
        raise ParseError("the request is not exactly {system, tools, messages}")
    if jcs.serialize_bytes(document) != payload:
        raise ParseError("the payload is not the RFC 8785 serialization of its request")
    messages = document["messages"]
    if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], dict):
        raise ParseError("messages is not exactly one message")
    message = messages[0]
    if set(message) != {"role", "content"} or message["role"] != "user":
        raise ParseError("the message is not {role: user, content}")
    if not isinstance(document["system"], list) or not isinstance(document["tools"], list):
        raise ParseError("system or tools is not a list")

    system = [_entry(e, "system") for e in document["system"]]
    tools = [_entry(e, "tools") for e in document["tools"]]
    counted = [e["text"] for e in document["system"]] + [e["text"] for e in document["tools"]]
    content = message["content"]
    if blocks:
        if not isinstance(content, list):
            raise ParseError("the message content is not a list of blocks")
        xml: list[Occurrence] = []
        for block in content:
            if not isinstance(block, dict) or set(block) not in ({"id", "text"}, {"id", "text", "conflict"}):
                raise ParseError("a content block is not {id, text[, conflict]}")
            parsed = _xml_stream(block["text"], allow_speaker=True)
            if len(parsed) != 1 or parsed[0].id != block["id"] or parsed[0].conflict != block.get("conflict"):
                raise ParseError(f"block {block.get('id')!r} does not hold exactly its own occurrence")
            xml.extend(parsed)
            counted.append(block["text"])
    else:
        if not isinstance(content, str):
            raise ParseError("the message content is not text")
        xml = _xml_stream(content, allow_speaker=True)
        counted.append(content)
    return Parsed(renderer, system + tools + xml, counted)


def parse(renderer: str, payload: bytes) -> Parsed:
    if renderer == "fixture-xml/v1":
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ParseError(f"not UTF-8: {error}") from None
        return Parsed(renderer, _xml_stream(text, allow_speaker=False), [text])
    if renderer == "cwa-messages/v1":
        return _messages(payload, blocks=False)
    if renderer == "cwa-message-blocks/v1":
        return _messages(payload, blocks=True)
    raise ParseError(f"no parser for renderer {renderer}")


def stream_of(wrap: str) -> tuple[str, str | None]:
    """The stream and tag a placement's wrap renders into."""
    if wrap == "system" or wrap == "tools":
        return wrap, None
    if wrap.startswith("xml:"):
        return "xml", wrap[4:]
    raise ParseError(f"wrap {wrap!r}")


def render_body(body: str, stream: str) -> str:
    """How a body appears in a stream: escaped inside an xml: wrap, as written in system and tools."""
    return escape_body(body) if stream == "xml" else body
