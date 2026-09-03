# Jiffle

Jiffle is a local application for importing, organizing, tagging, editing, and curating images and videos.

## Requirements

- Windows
- Python 3.11 or newer
- FFmpeg available in `PATH` for GIF/WebM conversion and oversized video export

## Installation

Open PowerShell in the Jiffle folder and start the application:

```powershell
python .\run.py
```

On the first run Jiffle checks for the Python packages it needs (Flask, Pillow, Requests, ImageHash, and OpenCV for video thumbnails and video imports) and installs any missing ones through pip. The first start therefore requires an internet connection and can take a few minutes. If you prefer to install everything up front, run:

```powershell
python -m pip install -r requirements.txt
```

For NVIDIA GPU background removal, install the CUDA 12.8 PyTorch builds in the same Python environment:

```powershell
python -m pip install --upgrade --force-reinstall `
  torch==2.11.0+cu128 `
  torchvision==0.26.0+cu128 `
  --extra-index-url https://download.pytorch.org/whl/cu128
```

Check the installation before selecting **GPU (CUDA)** in Settings:

```powershell
python -c "import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
```

The last line should print the NVIDIA device name. If it prints `CPU` or importing `torch` fails, choose **Automatic (recommended)** or **Processor (CPU)** until a CUDA-enabled build and compatible NVIDIA driver are installed.

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

### Update from GitHub

The updater does not require Git. It downloads the current application ZIP archive directly from GitHub:

`https://github.com/iban27830/jiffle/archive/refs/heads/master.zip`

1. Stop Jiffle by pressing `Ctrl+C` in its terminal window and wait for the process to exit.
2. Double-click `update.bat` in the Jiffle folder.
3. Allow the updater to finish downloading and checking the files. It then starts Jiffle again.

The updater requires Python (which Jiffle also needs) and Windows PowerShell 5.1 or newer. It creates a SQLite backup under `jiffle-data\migration-backups` before replacing application files. It does not replace `jiffle-data`, media folders, thumbnails, or the local `settings.json`. When a newer version needs database changes, Jiffle applies the pending migrations automatically during startup. Keep the backup until you have confirmed that the updated version works.

If the download or ZIP check fails, check the internet connection and GitHub access, then run `update.bat` again. If a migration or startup fails after an update, stop Jiffle, keep the backup folder, and restore the affected `jiffle-v2.db` backup before starting the previous version.

For an older installation whose updater still reports that Git is missing, download the current [`update.bat`](https://github.com/iban27830/jiffle/raw/refs/heads/master/update.bat?download=1) and replace the old file once. This replacement does not affect the library or its database.

## Using Jiffle

### Import media

1. Open **Import**.
2. Enter a local file path or a supported source URL.
3. Select **Import** or **Download**.
4. Review files that require confirmation under **Review**.

Supported online sources include:

- Danbooru
- e621 and e926 (post links such as `https://e621.net/posts/12345`, including query parameters)
- e621 and e926 post sets such as `https://e621.net/post_sets/12345` or `https://e926.net/post_sets/12345.json`
- Gelbooru
- FurAffinity

Some sources require account credentials configured under **Settings**. A direct image URL is also accepted; Jiffle downloads it, calculates its hash, and checks it for duplicates before placing it in the library or Review.

For an e621 or e926 post set, Jiffle first checks the complete set (using the saved e621 username and API key when configured), then downloads the posts one at a time in the set's order. The set itself is not added as a local collection. A private set or a set that cannot be accessed fails before any files are downloaded. A set that contains an unavailable post can finish partially; the Import history entry lists that post and the reason.

### Browse and organize

- Open **Library** to browse imported media and filter it by tags. When an online source provides an author, the author name appears on the media card and in the item details.
- On wide screens, the library list scrolls independently while the selected item's details and actions remain visible in the right panel. The right panel has its own scroll when its contents are taller than the window.
- Search for an exact library item with `id:123`. The media ID is shown in the item details and can be selected there to apply the same search.
- Images imported from e621/e926, Danbooru, or Gelbooru may show a parent marker on the thumbnail. Select it to open the imported parent when it is already in the library, or to search for the source post with `parent:123`. The item details panel shows the parent ID, an external source link, and categorized character tags when the source provides them.
- To fill in parent or character data for an older imported item, open its details and select **Refresh metadata**. The fetched result appears in **Review**; select **Apply** to add the new metadata and tags, or use the trash button to ignore it. The original file is not downloaded again.
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

Metadata refreshes for existing source-backed items also appear in **Review** as **Metadata update** entries. They are suggestions until you apply them, so a refresh cannot silently replace your current tags or parent relationship.

By default, previously deleted media is held in **Review** before it can be added again. To block it without asking, open **Settings**, enable **Never re-import deleted media**, and save the settings.

Set imports show their name, percentage, and processed count in the status bar. Select the stop button to finish the current download and stop before the next post. The final history entry is marked as completed, partial, or stopped and includes accepted, duplicate, Review, blocked, failed, and remaining counts. Expand **Issues** in a partial or failed entry to open each affected post and read its exact reason.

Set access uses the `e621_login` and `e621_api_key` values saved under **Settings → Sources → e621 / e926**. Jiffle sends them with Basic Authentication while checking the set metadata and all post pages; they are not included in the Import history.

### Configure sources

Open **Settings** to configure source accounts, library display options, storage folders, and tag rules.

### Background replacement

Background replacement is available for static images in **Library** and **Editor**. Jiffle looks for likely replacement candidates by checking whether a large connected area at the image edges is almost one color (white, black, or another uniform color). This scan is only a suggestion; always check the removal preview before saving a result.

To replace a background:

1. Open **Editor** and select **Find background candidates** to scan the whole library, or select an image in **Library** and choose **Replace background**. For one image, **Analyze selected** reports the estimated background area and color.
2. In **Settings → Background removal**, choose **Processing device**: **Automatic (recommended)** uses CUDA when a compatible NVIDIA GPU is available and otherwise uses the processor; **GPU (CUDA)** requires CUDA and reports a clear error if it is unavailable; **Processor (CPU)** always stays on the CPU. The response for a completed preview shows the actual device used. Choose **Removal model** separately: Automatic uses public **BiRefNet-HR** at 2048×2048 on CUDA and the faster public **BiRefNet** at 1024×1024 on CPU. You can choose **BiRefNet-HR** explicitly, use **BiRefNet** for speed, or select **RMBG-2.0 (compatibility)** for an existing RMBG installation. BiRefNet and BiRefNet-HR are public and do not require a token. RMBG-2.0 requires a Hugging Face `Read` token when its weights are not already cached; paste it into **Hugging Face token** and save settings. **Test access** checks the selected model, including public models without a token. The token itself is never shown back by the settings API.
3. In the image editor, open **Replace background** and select **Remove background / Preview**. The first request may take several minutes while Jiffle installs the local segmentation runtime (PyTorch, torchvision, transformers, safetensors, einops, kornia, and timm) and downloads the selected model weights. The BiRefNet-HR download is approximately 444 MB. Network access is required for setup; later requests use the cached model. A completed foreground preview is kept for the active image version, model, and device, so reopening the editor does not run the model again.
4. Automatic mode first tries BiRefNet-HR when CUDA is available, then falls back to standard BiRefNet if loading or inference fails. If an RMBG-2.0 snapshot is cached or a valid token is configured, RMBG is the final fallback. The editor reports the model that actually produced the preview. A CPU Automatic run starts with standard BiRefNet so processing remains practical.
5. Select **Choose background** to open the background library. Filter by category, or enter an import category and choose a local image file; imported backgrounds appear in the open modal and keep the selected category. Clicking a card only makes a temporary choice. Select **Select background** to confirm it, or use **Cancel**, **Escape**, or the backdrop to close the library without changing the current background.
6. Adjust **Blur** if needed, check the composition preview, and press **Apply background**. Jiffle creates and activates a full-resolution PNG version; the previous version remains available under **Versions** and can be restored later. Use **Cutout**, **Mask**, and **Zoom** to inspect the generated mask before applying it.
7. After the first run the action is named **Regenerate automatic mask**. It runs the selected model again. If the model produces the same mask, the editor reports that result; the existing preview can still be applied.

Background files and foreground previews are stored in the application's data directory and are not uploaded anywhere. A foreground preview belongs to the source image version, model, and device that produced it; after switching to another version or device, create a new preview for that combination.

If **Test access** reports an invalid token, create a new read token and replace the saved value. If it reports that access to RMBG-2.0 is denied, accept the model terms while logged in to Hugging Face. If it reports that Hugging Face is unavailable, check the network or proxy settings. Model setup requires enough disk space for PyTorch and the model cache. A GPU is optional; BiRefNet-HR is intended for GPU use and CPU processing is slower. If you select **GPU (CUDA)** while CUDA is unavailable, Jiffle returns `background.cuda_unavailable`; install a CUDA-enabled PyTorch build or select **Automatic (recommended)** or **Processor (CPU)**. If the preview reports that no usable subject was isolated, try another image or correct the source version before composing. If the editor reports that a preview is stale, restore the source version that produced it or run **Remove background / Preview** again.

## Troubleshooting

- If `python` is not recognized, install Python and enable **Add Python to PATH** during installation.
- If startup reports that a required package is missing, Jiffle normally installs it automatically on the next start when an internet connection is available. If automatic installation is blocked by permissions or the network, install the packages manually with `python -m pip install -r requirements.txt` in the Jiffle folder and restart Jiffle.
- If background removal reports that a runtime package is missing, keep Jiffle connected to the network and retry the action in the same Python environment used to start it. The error names the missing package, such as `einops`; install that package and restart Jiffle if automatic installation is blocked by permissions.
- If the page does not open automatically, visit `http://127.0.0.1:5001/` manually.
- If an online import fails, check the source credentials under **Settings** and confirm that the source URL is accessible in a browser.
- If Vision crop analysis fails, verify that the configured model accepts images and returns JSON coordinates. Local crop scanning and manual crop controls remain available without a model.
- If collection export reports that FFmpeg is unavailable, install FFmpeg, add its executable directory to `PATH`, and restart Jiffle. If a file cannot be reduced below its configured image or video limit, increase the corresponding per-file limit under **Settings → Import and limits**.

## License

Jiffle is distributed under the [MIT License](LICENSE).
