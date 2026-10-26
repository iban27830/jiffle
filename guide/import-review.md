# Import files and resolve Review

[Home](../README.md) · [Library](library.md) · [Source settings](settings.md#sources)

## Add media

Open **Import**, then do one of the following:

- Drop one or more images or videos, or use **Choose file** to select multiple files at once.
- Paste (`Ctrl+V`) an image, video, or one or more links anywhere on the Import screen.
- Paste one or more post links or direct media URLs (separated by newlines) and select **Resolve and import** or press **Enter**.
- Paste a supported e621/e926 post-set or pool URL, a Danbooru pool URL, or a multi-attachment Pawchive post URL to import its posts.

Pasting a URL alone does not submit it. After a local upload has been received, Jiffle keeps its own staged copy while resolving it; wait for the upload to finish before removing your original download. Direct media or CDN links are automatically checked against configured sources by hash and reverse search before falling back to Review. Blocked tags and tag aliases from **Settings → Tag rules** are applied automatically when importing or accepting sources.

To place an imported batch onto a personal shelf right away, tick **Add to shelf** on the Import screen. Choose an existing shelf (or use **Open selected shelf in Library** to inspect it first), or enter a name, pick a color, and select **+** to create a new shelf on the spot. Files accepted immediately, files later accepted from **Review**, and files that were already in the library under another shelf all receive the selected shelf.

Supported source adapters include Danbooru, e621/e926, Gelbooru, Rule34.xxx, FurAffinity, Twitter/X, Bluesky, JoyReactor, TBIB, and Pawchive, alongside reverse-image search via IQDB, SauceNAO, Fluffle, FuzzySearch, and Yandex. Some need credentials in **Settings → Sources**. A source that is not configured is skipped rather than blocking all other sources.

## Read Import history

| Status | Next step |
|---|---|
| **Importing** | Follow progress in the status bar |
| **Imported** | Use **Open in Library** to inspect the accepted file |
| **Already imported** | Open the existing library item; there is no need to import it again |
| **Waiting for review** | Open **Review** to choose what to keep |
| **Import failed** | Expand the entry for the reason, fix it, and retry |

Jiffle checks for exact copies and, for images, perceptually identical library matches. Ambiguous or approximate matches need a decision; it does not silently replace your high-resolution original. New tags or missing source metadata may be merged into an existing exact match.

Expand **Provider diagnostics** for the full search results. The small badges distinguish found candidates, sources needing an account, anonymous searches, and provider errors. **Not found** is not the same as **unavailable**. A failed source can appear alongside a successful result from another source.

Reverse-image searches send a small preview to the configured search services; a video is represented by a frame. Ordinary library browsing does not upload the collection anywhere.

## Choose a file in Review

1. Open **Review**. Use **All**, **Source found**, or **Needs source** to narrow the queue.
2. Select **Open full size** on a card. Cards do not accept files with an accidental click.
3. If candidates exist, compare the staged file with each candidate. Use arrows to switch candidates, **Previous item** / **Next item** in the header to step through the Review queue, and **Open source** to inspect the source page.
4. Choose one action (resolving an item automatically advances to the next card on the page):

   - **Use this source:** keep the displayed candidate and its metadata.
   - **Add source URL:** open the inline source URL field in the lightbox to provide a link yourself.
   - **Accept without choosing a source:** keep your staged file.
   - **Reject:** discard the pending group directly from the lightbox or the card.

A source selection can merge into an existing library item rather than creating a second copy. A likely duplicate may need a separate choice; read the comparison before accepting. In **Duplicates** (with the unresolved count shown in the sidebar), each pair shows resolution and file-size badges (**Higher resolution**, **Larger file**), and clicking a thumbnail opens it full size.

**Run the source search again** (or **Search again** when candidates were already found) rechecks one card. Tick several cards and use **Recheck selected** to queue them, or **Reject selected** to discard them in bulk. Sources that already returned no results are skipped until source settings or application logic change, so rechecking retries only the sources that timed out or were unavailable. Each card shows status badges for found candidates, incomplete searches (including alongside found candidates when another source failed), or no source found, while the log button shows the original import search and later rechecks grouped by stage, including clickable links when a reverse-search service matched an external page whose media file could not be downloaded automatically. **Select all on screen** only selects the current page.

Metadata refreshes appear separately as **Metadata update** suggestions. Select **Apply metadata** to accept the new information; **Ignore** dismisses the suggestion without replacing the file.

## Importing sets and large files

Post sets show progress, counts, and per-post issues in Import history. They do not automatically become a Jiffle collection. **Stop** prevents more posts from starting; already-running posts may finish. A failed or deleted post does not discard successful imports from the set.

Original GIF, MP4, and WebM files are stored without export conversion. The size limits in Settings apply to exported copies, not to the total library or upload. A source that stops sending data can still time out; retry it once connectivity is restored.

## After accepting a file

If crop review is enabled, newly accepted static images are checked locally. A file needing crop review remains in **Library**, but is excluded from collections until reviewed. Accepting a file is not an automatic crop. Videos and animations use the separate Trim settings. See [Editor](editor.md) for the decisions that clear each queue.
