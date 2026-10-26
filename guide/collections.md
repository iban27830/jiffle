# Build, resume, and export collections

[Home](../README.md) · [Search syntax](library.md#search) · [Editor](editor.md)

## Generate a selection

1. Open **Collections → Create**.
2. Enter a **Collection name**, **Count**, and **Search** query, such as `portrait blue_eyes -comic` (tag autocomplete suggests matching tags and `-tag` exclusions as you type).
3. Select **Generate**. Click any preview image to inspect it full size in the lightbox, and use **Replace** on individual items you do not want.
4. When the complete selection is ready, select **Save collection**.

The query supports the same tags, exclusions, aliases, and search operators (`shelf:`, `-shelf:`, `author:`, `-author:`, `source:`, `domain:`, `type:`, `parent:`, `has:`, `is:`, `orientation:`, `width:`, `height:`) as Library, and you can use the media-type icon at the start of the search box to filter by **All media**, **Static images**, **Videos**, **Animated / GIF**, or **Video + Animated (all moving)**. Author-limit warnings highlight the files involved. If the rules or count change, generate a new selection before saving.

In the **Collections** list, the search box filters saved collections by collection name, preset name, included or excluded tags, and item authors.

## Leave and return safely

The builder keeps one draft per browser tab: name, count, search, preset, exact selected order, and items rejected during replacement.

- **Open in Library** opens another tab by default, without changing your builder.
- If you choose same-tab links, Back or the Collections sidebar returns to your last screen.
- Refreshing the page restores the existing selection; it does not run Generate again.
- The **Collections** button returns to the list without deleting your draft. **Create** resumes it.
- **New draft** starts over after confirmation when there is work to discard.
- Saving clears only the draft you saved.

If a selected item was deleted or became ineligible, its place is kept and highlighted with a reason. Resolve the issue or explicitly replace it; Jiffle does not quietly substitute another image. A collection cannot be saved while its selection is invalid.

Drafts are not shared between tabs or devices and are not a permanent backup. Save a collection before ending the browser session or clearing site data.

## Reuse presets

Choose a preset to load its query and count. Enter a **Preset name** and use **Save preset** to create one. With a preset selected, saving updates it in place, including a renamed preset. The trash button deletes the selected preset.

Saved collections keep their creation rules and preset name as useful context. Changing a preset later does not regenerate an existing collection.

## View a saved collection

Open a collection from the list or card view. The detail view keeps its place when you visit another section and return.

- Click a static image to view it full size in the lightbox (or Ctrl/middle-click to open it in a new tab).
- Use **Export** or **ZIP** right from the collection header, or **Use rules** to open its query, preset, and count as a new draft in Collection Builder.
- The library icon opens the exact member in Library.
- The copy button copies one source link per item in gallery order, preferring **File source** when it differs.
- **URL Jiggie** stores a destination link with the collection.
- **Delete** removes the collection without deleting its library files.

## Export

Use **Export** for your configured destination or **ZIP** for a browser download. Choose **Browser download**, **Server folder**, or **Folder and download** in **Settings → Import and limits**.

Size limits apply to each exported file, not to the whole collection. New installations use 50 MB for each image and video and convert GIF/WebM to MP4. Edit conversion rows to suit the destination. Compressed or converted copies leave library originals untouched.

Export checks the current active versions. If crop review is required for a member, the error lists its media ID; review that file in Editor and retry. Jiffle does not silently omit blocked files or rewrite the saved collection.

When **Block exporting unreviewed clips** is enabled, pending Trim originals also block export. Confirm or split them first. Unlike the stricter crop rule, these clips can still be added manually, but cannot be exported until resolved.

For a size or conversion error, check FFmpeg and the per-file limits in Settings. A server-folder export is written on the machine running Jiffle, not automatically on the computer displaying the browser.
