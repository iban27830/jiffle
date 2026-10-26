# Find and organize media

[Home](../README.md) · [Editor](editor.md) · [Collections](collections.md)

## Search

Open **Library** and enter a query. Examples:

| Query | Find |
|---|---|
| `portrait blue_eyes` | Items with both tags |
| `portrait -comic` | Portraits without the comic tag |
| `author:artist_name` / `-author:artist_name` | Include or exclude items attributed to an author |
| `shelf:shelf_name` / `-shelf:shelf_name` | Include or exclude items on a personal shelf |
| `source:e621` / `domain:e621.net` | Filter by source provider or domain (`-source:` / `-domain:` to exclude) |
| `id:123` / `-id:123` | Include or exclude an exact library item ID |
| `parent:123` | Find a parent post and its child posts by source post ID |
| `type:image`, `type:video`, `type:animated`, `type:motion` | Filter by static image, video, animated GIF/WebP, or all moving media (video + animated) |
| `has:parent`, `has:children`, `has:family`, `has:source`, `has:shelf`, `is:edited`, `is:animated` | Filter by relationship, shelf, or revision state (`-has:` / `-is:` to exclude) |
| `orientation:portrait` (`landscape`, `square`) | Filter by aspect orientation |
| `width:>=1920` `height:>=1080` | Filter by pixel dimensions (`>`, `>=`, `<`, `<=`, `=`) |
| `text:keyword` | Match author, domain, source URL, or tag text |

Tag aliases apply to searches and collection rules. Suggestions appear at the cursor position as you type in the search bar (including `-tag` exclusions, tag aliases, substring matches, `shelf` / `has:shelf` / `-has:shelf`, and `author:`, `source:`, `domain:`, or `shelf:` prefixes), and tokens are highlighted inline inside the search box. Use the leading media-type icon in the search bar to filter by **All media**, **Static images**, **Videos**, **Animated / GIF**, or **Video + Animated (all moving)**, and the sort icon (**Newest first**, **Oldest first**, **Highest resolution**, **Largest file**, **Least used in collections**, **Most used in collections**, **By shelf**, or **Random**) to reorder results.

## Inspect an item

Select a thumbnail to open its details, and use the `←` and `→` arrow keys to move between items on the page. On a wide screen, the results and details panel scroll independently.

- Click the preview image in the inspector to view it full size in the lightbox, or use the full-size button to open the file itself in a new tab.
- **Source** identifies the post supplying metadata. **File source** appears separately when another provider supplied the actual file.
- **Media ID** opens an exact-ID search.
- **Open in Editor** chooses Crop/Background for a static image or Trim for a clip.
- **Replace background** opens that tool directly for a static image.
- **Refresh metadata** sends proposed changes to Review rather than applying them silently.

An edit marker means the active version is not the original. You can restore a previous version in Editor without deleting the others.

## Add personal tags and shelves

Type under **Add tag**. Suggestions appear after two characters. Select a suggestion or press Enter to create the tag. Personal tags have an **×** to remove them; source-provided tags are read-only.

Use **Shelves** to group media by your own themes with a custom color, independent of source tags:

- In the details panel, assign or remove shelves, change a shelf's color, or type a name and select **+** to create a new shelf.
- On gallery cards, colored dots indicate assigned shelves (hover a dot to see the shelf name).
- Tick cards in the gallery (or use the select-page icon button next to the search bar) and use the **Shelf** combo field (**+** / **−**) to add or remove a shelf across all selected items at once.
- Filter by shelf in the search bar (`shelf:name`, `-shelf:name`, `has:shelf`, `-has:shelf`), sort by **By shelf**, or delete a shelf everywhere using **×** next to its name.

## Parents, relatives, and duplicates

A parent marker can open the imported parent or search for its source ID. Character tags and parent details depend on the source metadata; use **Refresh metadata** for older items when needed.

Open **Duplicates** (the sidebar badge shows the current unresolved pair count), choose a similarity threshold, and select **Scan**. Each side displays its resolution, file size, author, and **Higher resolution** / **Larger file** badges; click either thumbnail to inspect it full size. A lower threshold includes more approximate matches. When two images are variations worth keeping, use **Family** instead of deleting one. Related items then link to one another in Library.

Video fragments are also related to their original. Their details identify **Part N of Media #X** and provide a route back to the source.

## Navigation without losing work

**Open in Library** links from collections, import history, and Editor open a new tab by default. They target the exact file, so your source tab stays where it was. You can still use Ctrl/Cmd-click or the middle mouse button as with an ordinary link.

To use the current tab instead, turn off **Settings → Interface → Open Library links in a new tab**. Browser Back/Forward and the sidebar then restore the relevant saved screen. Library filtering and selecting relatives remain normal in-tab actions.

Filters, selection, and many view preferences survive page refreshes. Collection and Editor drafts are tab-local, not synchronized between devices.
