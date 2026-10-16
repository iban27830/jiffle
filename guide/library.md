# Find and organize media

[Home](../README.md) · [Editor](editor.md) · [Collections](collections.md)

## Search

Open **Library** and enter a query. Examples:

| Query | Find |
|---|---|
| `portrait blue_eyes` | Items with both tags |
| `portrait -comic` | Portraits without the comic tag |
| `author:artist_name` / `-author:artist_name` | Include or exclude items attributed to an author |
| `source:e621` / `domain:e621.net` | Filter by source provider or domain (`-source:` / `-domain:` to exclude) |
| `id:123` / `-id:123` | Include or exclude an exact library item ID |
| `parent:123` | Find a parent post and its child posts by source post ID |
| `type:image`, `type:video`, `type:animated` | Filter by media type or animated GIF/WebP |
| `has:parent`, `has:children`, `has:family`, `has:source`, `is:edited`, `is:animated` | Filter by relationship or revision state (`-has:` / `-is:` to exclude) |
| `orientation:portrait` (`landscape`, `square`) | Filter by aspect orientation |
| `width:>=1920` `height:>=1080` | Filter by pixel dimensions (`>`, `>=`, `<`, `<=`, `=`) |
| `text:keyword` | Match author, domain, source URL, or tag text |

Tag aliases apply to searches and collection rules. Suggestions appear at the cursor position as you type in the search bar (including `-tag` exclusions, tag aliases, substring matches, and `author:`, `source:`, or `domain:` prefixes), and the clear button resets the query. Select a displayed tag or author to adjust the query. Clicking an author keeps your existing search terms. Use the media-type filter to separate images, videos, and animated GIFs, and use the sort menu (**Newest first**, **Oldest first**, **Highest resolution**, **Largest file**, **Least used in collections**, **Most used in collections**, or **Random**) to reorder results; library cards also show **Video** and **GIF** badges.

## Inspect an item

Select a thumbnail to open its details, and use the `←` and `→` arrow keys to move between items on the page. On a wide screen, the results and details panel scroll independently.

- Click the preview image in the inspector to view it full size in the lightbox, or use the full-size button to open the file itself in a new tab.
- **Source** identifies the post supplying metadata. **File source** appears separately when another provider supplied the actual file.
- **Media ID** opens an exact-ID search.
- **Open in Editor** chooses Crop/Background for a static image or Trim for a clip.
- **Replace background** opens that tool directly for a static image.
- **Refresh metadata** sends proposed changes to Review rather than applying them silently.

An edit marker means the active version is not the original. You can restore a previous version in Editor without deleting the others.

## Add personal tags

Type under **Add tag**. Suggestions appear after two characters. Select a suggestion or press Enter to create the tag. Personal tags have an **×** to remove them; source-provided tags are read-only.

## Parents, relatives, and duplicates

A parent marker can open the imported parent or search for its source ID. Character tags and parent details depend on the source metadata; use **Refresh metadata** for older items when needed.

Open **Duplicates** (the sidebar badge shows the current unresolved pair count), choose a similarity threshold, and select **Scan**. Each side displays its resolution, file size, author, and **Higher resolution** / **Larger file** badges; click either thumbnail to inspect it full size. A lower threshold includes more approximate matches. When two images are variations worth keeping, use **Family** instead of deleting one. Related items then link to one another in Library.

Video fragments are also related to their original. Their details identify **Part N of Media #X** and provide a route back to the source.

## Navigation without losing work

**Open in Library** links from collections, import history, and Editor open a new tab by default. They target the exact file, so your source tab stays where it was. You can still use Ctrl/Cmd-click or the middle mouse button as with an ordinary link.

To use the current tab instead, turn off **Settings → Interface → Open Library links in a new tab**. Browser Back/Forward and the sidebar then restore the relevant saved screen. Library filtering and selecting relatives remain normal in-tab actions.

Filters, selection, and many view preferences survive page refreshes. Collection and Editor drafts are tab-local, not synchronized between devices.
