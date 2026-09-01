# Jiffle

Jiffle is a local application for importing, organizing, tagging, editing, and curating images and videos.

## Requirements

- Windows
- Python 3.11 or newer
- FFmpeg available in `PATH` for GIF/WebM conversion and oversized video export

## Installation

Open PowerShell in the Jiffle folder and install the required packages:

```powershell
python -m pip install flask requests pillow
```

Install FFmpeg for export conversion, for example with Windows Package Manager:

```powershell
winget install Gyan.FFmpeg
```

Restart PowerShell after installation so `ffmpeg` is available in `PATH`.

## Running Jiffle

Start the application with:

```powershell
python .\run.py
```

You can also double-click `run.bat`.

Jiffle opens automatically in the default browser. If it does not, open:

```text
http://127.0.0.1:5001/
```

Keep the terminal window open while using the application. Press `Ctrl+C` in the terminal to stop Jiffle.

## Using Jiffle

### Import media

1. Open **Import**.
2. Enter a local file path or a supported source URL.
3. Select **Import** or **Download**.
4. Review files that require confirmation under **Review**.

Supported online sources include:

- Danbooru
- e621 and e926 (post links such as `https://e621.net/posts/12345`, including query parameters)
- Gelbooru
- FurAffinity

Some sources require account credentials configured under **Settings**. A direct image URL is also accepted; Jiffle downloads it, calculates its hash, and checks it for duplicates before placing it in the library or Review.

### Browse and organize

- Open **Library** to browse imported media and filter it by tags. When an online source provides an author, the author name appears on the media card and in the item details.
- Search for an exact library item with `id:123`. The media ID is shown in the item details and can be selected there to apply the same search.
- In the item details, use the full-size button to open the current file by itself in a new browser tab. **Open in Editor** opens the current version beside the original and shows the complete version history. Select **Analyze current** when you want a new crop proposal.
- Select an author in the item details to add an `author:name` filter without clearing the current search. The active author and selected media card remain highlighted while results update. Items with multiple authors show each author separately.
- Select an item to inspect its source, dimensions, tags, edits, and available actions. Images whose active version was changed in Editor have an edit icon on the library thumbnail; restoring the original removes the icon while keeping version history.
- Open **Duplicates**, choose a similarity threshold from 70% to 100%, and select **Scan** to find similar files. Lower values find more approximate matches; higher values restrict results to nearly identical images. Scan progress remains visible in the status bar while fingerprints and image pairs are processed.
- Open **Editor** to find images with removable empty margins. Use **Find crop candidates** for a background scan with progress and cancellation, or select an image in **Library** and choose **Open in Editor**.
- Use the library button beside a crop candidate or in the crop review screen to return to that image in **Library** with its details open.
- Completed scans remember images that produced no crop candidate, including skipped animations. Repeating a scan with the same detector settings skips their image analysis; changing minimum area, padding, or detector sensitivity makes them eligible for analysis again.
- Navigation state such as library filters, the selected image, editor status, crop coordinates, zoom, scroll position, and expanded Settings sections survives page refreshes for the current browser session. Unsaved Settings values are not stored.
- Review the source and cropped preview, adjust the Left, Top, Right, and Bottom coordinates, and select **Apply crop**. Jiffle asks for confirmation and retains the original and every accepted version. Use the Versions list to restore an earlier version, or **Reset to original** to make the original active without deleting later versions.
- The crop scan checks static images for almost-uniform white, black, colored, or transparent margins on any side. Animated images and videos are not scanned. **No crop needed** excludes the current version from later scans; **Skip** leaves it pending. Use **Reopen review** to reconsider an earlier decision. The coordinate reset button restores the saved crop proposal only.
- Configure the detector under **Settings → Crop analysis**: choose the cautious, normal, or sensitive preset, set the minimum removable area and retained padding, and select Local or Vision analysis for an image opened directly in Editor. Library-wide scans always use the local detector.
- Optional Vision analysis is available for one open image at a time. Configure the OpenAI-compatible or Gemini endpoint, model, and API key under **Settings → Crop vision model**, then select **Vision model** in the crop editor. The full original is sent only after this explicit action. An OpenAI-compatible URL may point to a local model server.
- Open **Collections** to create reusable selections and export them. In the collection builder, use one search field with the same syntax as Library: separate terms require all tags, prefix a term with `-` to exclude it, and use filters such as `author:name` when needed. Tag aliases apply to both included and excluded terms. Export limits apply to each file, not to the combined collection. By default, images and videos are each limited to 50 MB per file; oversized files are compressed only in the exported copy.
- The collection builder keeps the selected preset and tag rules with each saved collection. When viewing a collection, these rules are shown as creation metadata. Use the Library button on any preview or saved item to open that exact media ID in **Library**.
- Jiggie-compatible export conversions are configured under **Settings → Import and limits**. New installations convert GIF and WebM files to MP4 by default. Add, change, or remove conversion rows as needed; originals in the library are never changed.

### Import and review

Open **Import** and drop an image, video, or browser link into the drop area, or paste a supported source URL. The attempt appears in Import history immediately with an **Importing** status while downloading and parsing continue. Its status changes when processing finishes. Repeated imports of the same file or link do not add another item to **Review** when an identical file is already waiting there. Items needing a decision appear in **Review**, where the total and counts by reason are shown. Accept an item to add it to the library, provide a source when requested, or reject it.

By default, previously deleted media is held in **Review** before it can be added again. To block it without asking, open **Settings**, enable **Never re-import deleted media**, and save the settings.

### Configure sources

Open **Settings** to configure source accounts, library display options, storage folders, and tag rules.

### Background replacement

Background replacement is available for static images in **Library** and **Editor**. Jiffle looks for likely replacement candidates by checking whether a large connected area at the image edges is almost one color (white, black, or another uniform color). This scan is only a suggestion; always check the removal preview before saving a result.

To replace a background:

1. Open **Editor** and select **Find background candidates** to scan the whole library, or select an image in **Library** and choose **Replace background**. For one image, **Analyze selected** reports the estimated background area and color.
2. In the image editor, open **Replace background** and select **Remove background / Preview**. The first request may take several minutes while Jiffle installs the local RMBG-2.0 runtime and downloads its model weights. Network access is required only for this setup; later requests use the cached model.
3. After a successful preview, choose a background category and select an image from the local background library. To add a new background, enter its category, choose a local image file, and select **Import background**.
4. Adjust **Blur** and select **Apply background**. The blur control and apply action stay disabled until the foreground preview is ready and a background is selected.

The composition is saved as a new PNG version. The current/original version remains active, and the new version is shown under **Versions** where it can be activated or restored later. Background files are stored in the application's data directory and are not uploaded anywhere.

If model setup fails, check that Python can install packages and that the computer has network access and enough disk space for PyTorch and the model cache. A GPU is optional; CPU processing is supported but slower. If the preview reports that no usable subject was isolated, try another image or correct the source version before composing. A missing or expired preview must be generated again before applying a background.

## Troubleshooting

- If `python` is not recognized, install Python and enable **Add Python to PATH** during installation.
- If startup reports a missing module, run the installation command again in the same Python environment used to start Jiffle.
- If the page does not open automatically, visit `http://127.0.0.1:5001/` manually.
- If an online import fails, check the source credentials under **Settings** and confirm that the source URL is accessible in a browser.
- If Vision crop analysis fails, verify that the configured model accepts images and returns JSON coordinates. Local crop scanning and manual crop controls remain available without a model.
- If collection export reports that FFmpeg is unavailable, install FFmpeg, add its executable directory to `PATH`, and restart Jiffle. If a file cannot be reduced below its configured image or video limit, increase the corresponding per-file limit under **Settings → Import and limits**.

## License

Jiffle is distributed under the [MIT License](LICENSE).
