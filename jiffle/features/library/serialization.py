"""JSON shape shared by every endpoint that returns a media item."""

from jiffle.features.library.domain import MediaItem


def serialize_media(item: MediaItem) -> dict[str, object]:
    return {
        "id": item.id,
        "type": item.media_type.value,
        "source_url": item.source_url,
        "file_source_url": item.file_source_url,
        "author": item.author,
        "domain": item.domain,
        "width": item.width,
        "height": item.height,
        "file_size": item.file_size,
        "created_at": item.created_at,
        "active_revision_id": item.active_revision_id,
        "is_edited": bool(item.edit_operations),
        "edit_operations": list(item.edit_operations),
        "tags": list(item.tags),
        "manual_tags": list(item.manual_tags),
        "character_tags": list(item.character_tags),
        "characters": list(item.character_tags),
        "parent_id": item.parent_id,
        "parent_media_id": item.parent_media_id,
        "remote_id": item.remote_id,
        "parent_url": item.parent_url,
        "has_parent": bool(item.parent_id),
        "family_id": item.family_id,
        "relatives": list(item.relatives),
        "family_members": sorted((item.id, *item.relatives)) if item.family_id else [],
        "has_family": bool(item.family_id),
        "derived_from_media_id": item.derived_from_media_id,
        "trim_start_ms": item.trim_start_ms,
        "trim_end_ms": item.trim_end_ms,
        "trim_index": item.trim_index,
        "fragment_count": item.fragment_count,
        "auto_collection_excluded": item.auto_collection_excluded,
        "is_animated": item.is_animated,
        "content_url": f"/api/v1/media/{item.id}/content?revision={item.active_revision_id or 0}",
        "thumbnail_url": f"/api/v1/media/{item.id}/thumbnail?revision={item.active_revision_id or 0}",
    }
