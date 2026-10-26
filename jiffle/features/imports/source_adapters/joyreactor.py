"""JoyReactor / Reactor.cc post and image source adapter via GraphQL API."""

import base64
from pathlib import PurePosixPath
import re
from urllib.parse import parse_qs, unquote, urlparse

import requests

from jiffle.features.imports.source_adapters.contracts import SourceMedia
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.source_adapters.platform_tags import add_platform_tags

JOYREACTOR_GRAPHQL_ENDPOINT = "https://api.joyreactor.cc/graphql"
JOYREACTOR_POST_DOMAINS = frozenset({
    "joyreactor.cc",
    "www.joyreactor.cc",
    "m.joyreactor.cc",
    "old.joyreactor.cc",
    "furry.reactor.cc",
    "reactor.cc",
    "www.reactor.cc",
    "m.reactor.cc",
    "old.reactor.cc",
    "pornreactor.cc",
    "www.pornreactor.cc",
    "joyreactor.com",
    "www.joyreactor.com",
})

_POST_PATH_RE = re.compile(r"/post/(\d+)")
_THUMB_POST_RE = re.compile(r"/pics/thumbnail/post-(\d+)\.([a-zA-Z0-9]+)")
_ATTR_IMAGE_RE = re.compile(r"/pics/post/(?:full/)?.*?(?:-|/post-)(\d+)\.([a-zA-Z0-9]+)$")
_HREF_RE = re.compile(r'href=["\'](https?://[^"\'>\s]+)["\']', re.I)

_ARTIST_CATEGORY_NAMES = frozenset({"artist", "furry artist", "художник", "автор"})
_NON_ARTIST_ROOT_TAGS = frozenset({
    "furry",
    "фурри",
    "фэндомы",
    "песочница",
    "разное",
    "art",
    "арт",
    "красивые картинки",
    "эротика",
    "хентай",
    "anime",
    "аниме",
    "комиксы",
    "гифки",
    "видео",
    "приколы",
    "мемы",
    "животные",
    "искусство",
    "nsfw",
    "sfw",
})

_POST_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on Post {
      id
      text
      tags {
        name
        category {
          name
          category {
            name
          }
        }
      }
      attributes {
        ... on PostAttributePicture {
          id
          image {
            type
            width
            height
          }
        }
      }
    }
  }
}
"""

_ATTR_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on PostAttributePicture {
      id
      image {
        type
        width
        height
      }
      post {
        id
        text
        tags {
          name
          category {
            name
            category {
              name
            }
          }
        }
        attributes {
          ... on PostAttributePicture {
            id
            image {
              type
              width
              height
            }
          }
        }
      }
    }
  }
}
"""

_IMAGE_TYPE_EXTENSION = {
    "JPEG": "jpeg",
    "JPG": "jpeg",
    "PNG": "png",
    "GIF": "gif",
    "WEBP": "webp",
    "MP4": "mp4",
    "WEBM": "webm",
}


class JoyReactorSourceProvider:
    provider_name = "joyreactor"
    supports_reverse_search = False
    domains = JOYREACTOR_POST_DOMAINS
    endpoint = JOYREACTOR_GRAPHQL_ENDPOINT

    def can_handle(self, url: str) -> bool:
        return _parse_joyreactor_url(url) is not None

    def can_handle_set(self, _url: str) -> bool:
        return False

    def _graphql(self, query: str, node_id: str) -> dict | None:
        try:
            response = requests.post(
                self.endpoint,
                json={"query": query, "variables": {"id": node_id}},
                headers={"User-Agent": "Jiffle/2.0", "Accept": "application/json"},
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status == 404:
                raise SourceProviderFailure(
                    "import.source_post_not_found", "The JoyReactor post was not found."
                ) from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "JoyReactor metadata could not be loaded."
            ) from error
        except (requests.RequestException, ValueError, TypeError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "JoyReactor metadata could not be loaded."
            ) from error
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        return data.get("node") if isinstance(data, dict) and isinstance(data.get("node"), dict) else None

    def fetch_post_node(self, url: str) -> tuple[dict | None, str | None, str | None, str | None]:
        """Return ``(post_node, post_id, attr_id, ext)`` for a JoyReactor URL."""
        parsed = _parse_joyreactor_url(url)
        if parsed is None:
            raise SourceProviderFailure(
                "import.invalid_source_url", "The URL is not a JoyReactor post or image URL."
            )
        post_id, attr_id, ext = parsed
        if post_id:
            encoded = base64.b64encode(f"Post:{post_id}".encode("utf-8")).decode("ascii")
            node = self._graphql(_POST_QUERY, encoded)
            if not isinstance(node, dict) or not node.get("id"):
                raise SourceProviderFailure(
                    "import.source_post_not_found", "The JoyReactor post was not found."
                )
            return node, post_id, attr_id, ext
        if attr_id:
            encoded = base64.b64encode(
                f"PostAttributePicture:{attr_id}".encode("utf-8")
            ).decode("ascii")
            node = self._graphql(_ATTR_QUERY, encoded)
            if isinstance(node, dict):
                post_node = node.get("post") if isinstance(node.get("post"), dict) else None
                decoded_post_id = _decode_global_id(post_node.get("id")) if post_node else None
                img_ext = _image_ext(node.get("image")) or ext
                return post_node, decoded_post_id, attr_id, img_ext
            return None, None, attr_id, ext
        raise SourceProviderFailure(
            "import.invalid_source_url", "The URL is not a JoyReactor post or image URL."
        )

    def fetch(self, url: str) -> SourceMedia:
        post_node, post_id, attr_id, fallback_ext = self.fetch_post_node(url)
        chosen_attr_id = attr_id
        chosen_ext = fallback_ext or "jpeg"
        if isinstance(post_node, dict):
            resolved_post_id = _decode_global_id(post_node.get("id")) or post_id
            attributes = post_node.get("attributes")
            if isinstance(attributes, list):
                picture_attrs = [
                    attr for attr in attributes
                    if isinstance(attr, dict) and attr.get("id") and isinstance(attr.get("image"), dict)
                ]
                matched_attr = None
                if chosen_attr_id:
                    matched_attr = next(
                        (
                            attr for attr in picture_attrs
                            if _decode_global_id(attr.get("id")) == chosen_attr_id
                        ),
                        None,
                    )
                if matched_attr is None and picture_attrs:
                    matched_attr = picture_attrs[0]
                if matched_attr is not None:
                    chosen_attr_id = _decode_global_id(matched_attr.get("id")) or chosen_attr_id
                    chosen_ext = _image_ext(matched_attr.get("image")) or chosen_ext
            if not chosen_attr_id:
                raise SourceProviderFailure(
                    "import.source_media_missing",
                    "The JoyReactor post has no downloadable media.",
                )
            author, tags = _extract_author_and_tags(post_node.get("tags"))
            direct_url = f"https://img10.reactor.cc/pics/post/full/post-{chosen_attr_id}.{chosen_ext}"
            canonical_url = (
                f"https://joyreactor.cc/post/{resolved_post_id}"
                if resolved_post_id
                else direct_url
            )
            file_ext = ".jpg" if chosen_ext == "jpeg" else f".{chosen_ext}"
            return SourceMedia(
                canonical_url=canonical_url,
                direct_media_url=direct_url,
                provider=self.provider_name,
                remote_id=str(resolved_post_id or chosen_attr_id),
                author=author,
                domain="joyreactor.cc",
                tags=tags,
                file_extension=file_ext,
            )
        if chosen_attr_id:
            direct_url = f"https://img10.reactor.cc/pics/post/full/post-{chosen_attr_id}.{chosen_ext}"
            file_ext = ".jpg" if chosen_ext == "jpeg" else f".{chosen_ext}"
            return SourceMedia(
                canonical_url=f"https://joyreactor.cc/post/{post_id}" if post_id else direct_url,
                direct_media_url=direct_url,
                provider=self.provider_name,
                remote_id=str(post_id or chosen_attr_id),
                author=None,
                domain="joyreactor.cc",
                tags=(),
                file_extension=file_ext,
            )
        raise SourceProviderFailure(
            "import.source_post_not_found", "The JoyReactor post was not found."
        )

    def fetch_metadata(self, url: str) -> SourceMedia:
        return self.fetch(url)

    def search_similar(self, _image_path):
        return []

    def check_connection(self) -> None:
        encoded = base64.b64encode(b"Post:1").decode("ascii")
        self._graphql("query($id: ID!) { node(id: $id) { id } }", encoded)


def _is_reactor_host(hostname: str) -> bool:
    host = (hostname or "").lower().strip(".")
    if host in JOYREACTOR_POST_DOMAINS:
        return True
    return host.endswith((".reactor.cc", ".joyreactor.cc", ".joyreactor.com", ".pornreactor.cc"))


def _parse_joyreactor_url(url: str) -> tuple[str | None, str | None, str | None] | None:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not _is_reactor_host(parsed.hostname or ""):
        return None
    path = unquote(parsed.path or "")
    query = parse_qs(parsed.query)
    wanted_attr = (query.get("attr") or query.get("attribute") or [None])[0]
    wanted_attr = str(wanted_attr).strip() if wanted_attr and str(wanted_attr).strip().isdigit() else None

    post_match = _POST_PATH_RE.search(path)
    if post_match:
        return post_match.group(1), wanted_attr, None

    thumb_match = _THUMB_POST_RE.search(path)
    if thumb_match:
        return thumb_match.group(1), wanted_attr, thumb_match.group(2).lower()

    attr_match = _ATTR_IMAGE_RE.search(path)
    if attr_match:
        return None, attr_match.group(1), attr_match.group(2).lower()
    return None


def _decode_global_id(encoded: object) -> str | None:
    text = str(encoded or "").strip()
    if not text:
        return None
    try:
        decoded = base64.b64decode(text).decode("utf-8")
    except Exception:
        return None
    if ":" in decoded:
        _, raw_id = decoded.split(":", 1)
        return raw_id.strip() or None
    return decoded.strip() or None


def _image_ext(image_obj: object) -> str | None:
    if not isinstance(image_obj, dict):
        return None
    img_type = str(image_obj.get("type") or "").strip().upper()
    return _IMAGE_TYPE_EXTENSION.get(img_type)


def _category_chain(tag_obj: dict) -> list[str]:
    chain: list[str] = []
    current = tag_obj.get("category")
    depth = 0
    while isinstance(current, dict) and depth < 5:
        name = str(current.get("name") or "").strip()
        if name:
            chain.append(name.lower())
        current = current.get("category")
        depth += 1
    return chain


def _extract_author_and_tags(raw_tags: object) -> tuple[str | None, tuple[str, ...]]:
    if not isinstance(raw_tags, list):
        return None, ()
    parent_categories: set[str] = set()
    tag_entries: list[tuple[str, list[str]]] = []
    for item in raw_tags:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        chain = _category_chain(item)
        parent_categories.update(chain)
        tag_entries.append((name, chain))

    author: str | None = None
    for name, chain in tag_entries:
        lowered = name.lower()
        if lowered in _ARTIST_CATEGORY_NAMES:
            continue
        if any(cat in _ARTIST_CATEGORY_NAMES for cat in chain):
            author = name
            break

    if author is None:
        for name, chain in tag_entries:
            lowered = name.lower()
            if (
                not chain
                and lowered not in parent_categories
                and lowered not in _NON_ARTIST_ROOT_TAGS
            ):
                author = name
                break

    normalized_tags: list[str] = []
    has_furry = False
    for name, chain in tag_entries:
        cleaned = name.strip().lower().replace(" ", "_")
        if cleaned:
            normalized_tags.append(cleaned)
        if cleaned in {"furry", "фурри", "furrotica", "furotica"} or "furry" in chain:
            has_furry = True
    if has_furry and "furry" not in normalized_tags:
        normalized_tags.append("furry")
    tags = add_platform_tags(
        tuple(dict.fromkeys(normalized_tags)), "joyreactor", "joyreactor.cc"
    )
    return author, tags


def extract_external_source_links(post_text: object) -> list[str]:
    """Extract external source links (Twitter/X, FurAffinity, e621, Bluesky, etc.) from JoyReactor post HTML."""
    text = str(post_text or "")
    if not text:
        return []
    cleaned = text.replace("\\/", "/")
    links: list[str] = []
    for raw_url in _HREF_RE.findall(cleaned):
        url = raw_url.strip()
        host = (urlparse(url).hostname or "").lower()
        if not host or _is_reactor_host(host):
            continue
        links.append(url)
    return list(dict.fromkeys(links))
