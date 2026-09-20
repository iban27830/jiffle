"""Normalize source credentials that were pasted as a ready-made query string.

Account pages such as rule34.xxx show the API credentials as one line
(``&api_key=...&user_id=...``).  Users frequently paste that whole line into a
single settings field instead of splitting it, so the settings layer extracts
the known key/value pairs and fills the individual fields instead of rejecting
the input.
"""

from urllib.parse import parse_qs


# Each group lists the settings fields that belong to one source account.
CREDENTIAL_GROUPS: tuple[tuple[str, ...], ...] = (
    ("rule34_user_id", "rule34_api_key"),
    ("gelbooru_user_id", "gelbooru_api_key"),
    ("danbooru_login", "danbooru_api_key"),
    ("e621_login", "e621_api_key"),
)

# Settings field -> parameter name accepted inside a pasted credential string.
PARAMETER_FIELDS = {
    "rule34_user_id": "user_id",
    "rule34_api_key": "api_key",
    "gelbooru_user_id": "user_id",
    "gelbooru_api_key": "api_key",
    "danbooru_login": "login",
    "danbooru_api_key": "api_key",
    "e621_login": "login",
    "e621_api_key": "api_key",
}


def absorb_pasted_credentials(values: dict) -> dict:
    """Move ``key=value`` pairs pasted into credential fields to their own fields.

    ``values`` is the pending settings update; it is modified in place and
    returned.  Fields whose value is not a recognizable query string keep the
    text the user typed.
    """
    for fields in CREDENTIAL_GROUPS:
        present = [field for field in fields if field in values]
        if not present:
            continue
        text = " ".join(
            str(values[field]) for field in present if values[field] is not None
        )
        extracted = extract_credentials(text)
        if not extracted:
            continue
        for field in fields:
            parameter = PARAMETER_FIELDS[field]
            if parameter in extracted:
                values[field] = extracted[parameter]
    return values


def extract_credentials(text: str) -> dict[str, str]:
    """Return the known query parameters found in a pasted credential string.

    The input may be a bare fragment (``&api_key=...&user_id=...``), a query
    string, or a full account URL.  Anything that is not a ``key=value`` pair is
    ignored, so a plainly typed login or API key passes through unchanged.
    """
    if not text or "=" not in text:
        return {}
    normalized = (
        text.replace("&amp;", "&")
        .replace("\r", "")
        .replace("\n", "&")
        .replace("?", "&")
        .strip()
        .lstrip("&")
    )
    try:
        parsed = parse_qs(normalized, keep_blank_values=True)
    except ValueError:
        return {}
    credentials: dict[str, str] = {}
    for key, raw_values in parsed.items():
        name = key.strip().lower()
        for raw_value in raw_values:
            value = raw_value.strip()
            if value and name not in credentials:
                credentials[name] = value
    return credentials
