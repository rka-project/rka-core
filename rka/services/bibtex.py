"""Bounded, non-executing BibTeX subset for the dependency-free Core install.

The academic extra remains the full parser. This fallback handles nested braced
and quoted values, numeric literals and comments. Unsupported string macros or
concatenation fail explicitly instead of silently truncating research metadata.
"""

from __future__ import annotations

import re


class BibtexParseError(ValueError):
    """Input cannot be faithfully parsed; no entries should be imported."""


class _Reader:
    def __init__(self, text: str):
        self.text = text
        # A UTF-8 BOM is a file marker, not an implicit-comment line. Keep the
        # original string so raw entry slices retain their exact source text.
        self.pos = 1 if text.startswith("\ufeff") else 0

    def fail(self, reason: str):
        raise BibtexParseError(f"{reason} at character {self.pos}")

    def space(self):
        while self.pos < len(self.text):
            if self.text[self.pos].isspace():
                self.pos += 1
            elif self.text[self.pos] == "%":
                end = self.text.find("\n", self.pos)
                self.pos = len(self.text) if end < 0 else end + 1
            else:
                break

    def expect(self, character: str):
        self.space()
        if not self.text.startswith(character, self.pos):
            self.fail(f"Expected {character!r}")
        self.pos += len(character)

    def token(self) -> str:
        self.space()
        match = re.compile(r"[\w:.-]+", re.ASCII).match(self.text, self.pos)
        if match is None:
            self.fail("Expected a BibTeX identifier")
        self.pos = match.end()
        return match.group()

    def enclosed(self) -> str:
        opener = self.text[self.pos]
        self.pos += 1
        start = self.pos
        depth = 1 if opener == "{" else 0
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "\\":
                self.pos += 2
                continue
            if char == "{":
                depth += 1
                if depth > 64:
                    self.fail("BibTeX nesting limit exceeded")
            elif char == "}":
                depth -= 1
                if depth < 0:
                    self.fail("Unexpected closing brace")
                if opener == "{" and depth == 0:
                    value = self.text[start : self.pos]
                    self.pos += 1
                    return value
            elif char == '"' and opener == '"' and depth == 0:
                value = self.text[start : self.pos]
                self.pos += 1
                return value
            self.pos += 1
        self.fail("Unterminated BibTeX value")

    def value(self) -> str:
        self.space()
        if self.pos >= len(self.text):
            self.fail("Missing field value")
        if self.text[self.pos] in '{"':
            value = self.enclosed()
        else:
            value = self.token()
            if not value.isdecimal():
                self.fail("String macros require rka-core[academic]")
        self.space()
        if self.text.startswith("#", self.pos):
            self.fail("String concatenation requires rka-core[academic]")
        return value


def parse_basic_bibtex(content: str) -> list[dict]:
    """Return raw field dictionaries and source spans, or one explicit error.

    Non-entry text is a BibTeX implicit comment. Malformed @ blocks, repeated
    keys/fields and unsupported constructs are never accepted as partial data.
    The caller applies the shared input-byte ceiling before parsing.
    """
    reader = _Reader(content)
    entries = []
    keys = set()
    while reader.pos < len(content):
        reader.space()
        if reader.pos >= len(content):
            break
        if content[reader.pos] != "@":
            # Implicit comments are line-oriented here: do not interpret an
            # email address in prose as a citation block.
            end = content.find("\n", reader.pos)
            reader.pos = len(content) if end < 0 else end + 1
            continue
        start = reader.pos
        reader.pos += 1
        kind = reader.token().lower()
        reader.space()
        if reader.pos >= len(content) or content[reader.pos] not in "{(":
            reader.fail("Expected an entry opener")
        opener = content[reader.pos]
        if kind == "comment":
            if opener != "{":
                reader.fail("Parenthesized comments require rka-core[academic]")
            reader.enclosed()
            continue
        if kind in {"string", "preamble"}:
            reader.fail("String macros and preambles require rka-core[academic]")
        reader.pos += 1
        closer = "}" if opener == "{" else ")"
        key = reader.token()
        if key in keys:
            reader.fail("Duplicate citation key")
        keys.add(key)
        reader.expect(",")
        fields = {}
        while True:
            reader.space()
            if content.startswith(closer, reader.pos):
                reader.pos += 1
                break
            name = reader.token().lower()
            if name in fields:
                reader.fail("Duplicate field name")
            reader.expect("=")
            fields[name] = reader.value()
            if content.startswith(closer, reader.pos):
                reader.pos += 1
                break
            reader.expect(",")
        entries.append({"fields": fields, "raw": content[start : reader.pos]})
    return entries
