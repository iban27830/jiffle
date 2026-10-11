# Configure Jiffle

[Home](../README.md) · [Editor](editor.md) · [Troubleshooting](troubleshooting.md)

Open **Settings** and expand the relevant section. Use **Save settings** for application settings. Browser display preferences apply immediately. Unsaved application-setting edits are not restored after a refresh.

## Interface

| Setting | Scope |
|---|---|
| **Open Library links in a new tab** | This browser; on by default |
| Font size, cards per page, card size, labels | This browser |
| Collection and Editor drafts | This tab's session |

Turning off new-tab links does not disable draft restoration. Ctrl/Cmd-click and the middle mouse button still work on Library links.

## Import and limits

| Setting | What it does |
|---|---|
| **Never re-import deleted media** | Blocks a previously deleted file instead of asking through Review |
| **Check crop before using new images in collections** | Checks new static images and requires unresolved crops to be reviewed before collection use; off by default |
| **Review videos and animations for trimming** | Enrolls clips in Trim review, including existing clips when enabled; off by default |
| **Block exporting unreviewed clips** | Excludes pending clips from automatic selection and blocks their export; off by default |
| **Items per author** | Sets the collection author limit |
| **Maximum image/video file, MB** | Per-file export limits, not import limits |
| **Export destination / format conversions** | Selects folder/download delivery and output formats |

Crop and Trim are separate controls. Crop checking does not automatically scan old unchecked images, does not crop anything without a decision, and does not alter your Trim settings. See [crop decisions](editor.md#keep-unchecked-crops-out-of-collections).

## Sources

Configure the accounts you use, then select **Test** beside each source:

- Danbooru: login and API key.
- e621/e926: username and API key.
- Gelbooru and Rule34.xxx: User ID and API key.
- FurAffinity: cookies `a` and `b` from an active browser session.

For Gelbooru/Rule34, you can paste the account's `&api_key=…&user_id=…` line into either field; Jiffle separates the pair when saving. A missing half of a credential pair is reported rather than silently treated as a working account.

**Test** distinguishes metadata access from reverse-search access. A source can support one without the other. FurAffinity's **Open login** link opens on your browsing computer, not on the server. Never share keys or cookies in screenshots or exported notes.

**Fill implicit tags** fills missing source-derived tags, such as `furry`, for relevant older items. Use it deliberately; normal browsing does not re-import the whole library.

## Crop and background processing

- **Crop analysis:** sensitivity, minimum removable area, retained padding, and the selected single-image analysis method.
- **Crop vision model:** provider format, endpoint, model, and API key for optional explicit image analysis. Local checks work without it.
- **Background removal:** removal model, device, and optional model-access token. This does not enable local AI processing when the NAS configuration disables it.

## Storage and tag rules

Storage paths are on the machine running Jiffle. In Docker, use the mounted container paths, not the drive letters on your browsing computer. Do not change them casually for an existing library; keep a backup and ensure existing files remain accessible.

Tag rules let you manage exclusions and aliases. Aliases affect Library searches and collection selection. They do not remove the original image files.
