"""Normalization rules for tags a user types into the Library inspector."""

import re

MAX_TAG_LENGTH = 200

_WHITESPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def normalize_tag(raw: object) -> str:
    """Return the canonical form of a manually typed tag or raise ``ValueError``.

    The library already stores tags lowercase with underscores instead of
    spaces, so manual tags follow the same rules: surrounding quotes and
    whitespace are trimmed, internal whitespace collapses to a single ``_``.
    A leading ``-`` is rejected because the search field reads it as an
    exclusion prefix.
    """
    value = "" if raw is None else str(raw)
    value = value.strip().strip('"').strip().strip("'").strip()
    value = _WHITESPACE.sub("_", value).lower()
    value = value.strip("_")
    if not value:
        raise ValueError("A tag must not be empty.")
    if _CONTROL.search(value):
        raise ValueError("A tag must not contain control characters.")
    if value.startswith("-"):
        raise ValueError("A tag must not start with '-'.")
    if len(value) > MAX_TAG_LENGTH:
        raise ValueError(f"Tags must not exceed {MAX_TAG_LENGTH} characters.")
    return value


def escape_glob_prefix(prefix: str) -> str:
    """Escape SQLite GLOB metacharacters so a typed prefix is matched literally."""
    escaped = []
    for character in prefix:
        if character in "*?[":
            escaped.append(f"[{character}]")
        elif character == "]":
            escaped.append("[]]")
        else:
            escaped.append(character)
    return "".join(escaped) + "*"


def normalize_tag_prefix(raw: object) -> str:
    """Lenient normalization for the suggestion query; never raises."""
    value = "" if raw is None else str(raw)
    value = _WHITESPACE.sub("_", value.strip().strip('"').strip()).lower()
    return value.strip("_")
