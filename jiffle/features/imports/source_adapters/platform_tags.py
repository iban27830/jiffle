"""Implicit tags that belong to every post of a furry-only source.

e621/e926 and FurAffinity host furry artwork only, so a post from these sites
is tagged ``furry`` even when the site's own tag list for that post omits it.
A general booru (Rule34, Gelbooru, Danbooru, TBIB) can carry the same signal in
its post ``source`` field, which usually points back at the original e621 or
FurAffinity upload.  Those links are treated as furry too.

Keeping the rule in one helper lets every import path (single post, post set,
exact/perceptual match, and Review acceptance) apply the same tags.
"""

from urllib.parse import urlsplit

FURRY_TAG = "furry"

# Provider names as stored in ``media_sources.provider``.
FURRY_ONLY_PROVIDERS = frozenset({"e621", "furaffinity"})

# Hosts that carry furry artwork only, without the ``www.`` prefix.
FURRY_ONLY_DOMAINS = frozenset({"e621.net", "e926.net", "furaffinity.net"})


def _normalized_host(value: object) -> str:
    """Return a bare, lower-case host for a URL or a bare domain."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "//" + text
    host = (urlsplit(text).hostname or "").strip().strip(".").lower()
    return host[4:] if host.startswith("www.") else host


def _hosts_from_urls(urls) -> set[str]:
    """Return the hosts mentioned by a booru ``source`` field.

    Booru sources are whitespace-separated URL lists, but a single URL or any
    iterable of them is accepted as well.
    """
    if not urls:
        return set()
    tokens = urls.split() if isinstance(urls, str) else [
        token for value in urls for token in str(value).split()
    ]
    return {host for host in (_normalized_host(token) for token in tokens) if host}


def is_furry_only_source(provider: str | None = None, domain: str | None = None, urls=()) -> bool:
    """Whether a post is known to be furry from its site or its source links."""
    if str(provider or "").strip().lower() in FURRY_ONLY_PROVIDERS:
        return True
    if _normalized_host(domain) in FURRY_ONLY_DOMAINS:
        return True
    return bool(_hosts_from_urls(urls) & FURRY_ONLY_DOMAINS)


def platform_tags(
    provider: str | None = None, domain: str | None = None, urls=()
) -> tuple[str, ...]:
    """Return the tags every import from this source must carry."""
    return (FURRY_TAG,) if is_furry_only_source(provider, domain, urls) else ()


def add_platform_tags(
    tags, provider: str | None = None, domain: str | None = None, urls=()
) -> tuple[str, ...]:
    """Append the missing implicit platform tags to an import's tag list.

    Matching is case-insensitive so a source that already carries ``Furry``
    does not gain a duplicate ``furry``.  ``urls`` is the ``source`` field of a
    booru post: when it points at e621/e926 or FurAffinity the post is furry.
    """
    values = tuple(str(tag) for tag in tags)
    existing = {value.strip().lower() for value in values}
    additions = tuple(
        tag for tag in platform_tags(provider, domain, urls) if tag.lower() not in existing
    )
    return values + additions
