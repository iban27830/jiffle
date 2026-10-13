# Find and organize media

[Home](../README.md) · [Editor](editor.md) · [Collections](collections.md)

## Search

Open **Library** and enter a query. Examples:

| Query | Find |
|---|---|
| `portrait blue_eyes` | Items with both tags |
| `portrait -comic` | Portraits without the comic tag |
| `author:artist_name` | Items attributed to an author |
| `id:123` | One exact library item |
| `parent:123` | Find a parent post in the library by its source post ID |

Tag aliases apply to searches and collection rules. Select a displayed tag or author to adjust the query. Clicking an author keeps your existing search terms. Use the media-type filter to separate images and videos.

## Inspect an item

Select a thumbnail to open its details. On a wide screen, the results and details panel scroll independently.

- **Source** identifies the post supplying metadata. **File source** appears separately when another provider supplied the actual file.
- **Media ID** opens an exact-ID search.
- The full-size button opens the file itself in a new tab.
- **Open in Editor** chooses Crop/Background for a static image or Trim for a clip.
- **Replace background** opens that tool directly for a static image.
- **Refresh metadata** sends proposed changes to Review rather than applying them silently.

An edit marker means the active version is not the original. You can restore a previous version in Editor without deleting the others.

## Add personal tags

Type under **Add tag**. Suggestions appear after two characters. Select a suggestion or press Enter to create the tag. Personal tags have an **×** to remove them; source-provided tags are read-only.

## Parents, relatives, and duplicates

A parent marker can open the imported parent or search for its source ID. Character tags and parent details depend on the source metadata; use **Refresh metadata** for older items when needed.

Open **Duplicates**, choose a similarity threshold, and select **Scan**. A lower threshold includes more approximate matches. When two images are variations worth keeping, use **Family** instead of deleting one. Related items then link to one another in Library.

Video fragments are also related to their original. Their details identify **Part N of Media #X** and provide a route back to the source.

## Navigation without losing work

**Open in Library** links from collections, import history, and Editor open a new tab by default. They target the exact file, so your source tab stays where it was. You can still use Ctrl/Cmd-click or the middle mouse button as with an ordinary link.

To use the current tab instead, turn off **Settings → Interface → Open Library links in a new tab**. Browser Back/Forward and the sidebar then restore the relevant saved screen. Library filtering and selecting relatives remain normal in-tab actions.

Filters, selection, and many view preferences survive page refreshes. Collection and Editor drafts are tab-local, not synchronized between devices.
