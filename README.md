# Jiffle

Jiffle is a local application for importing, organizing, tagging, editing, and curating images and videos.

## Requirements

- Windows
- Python 3.11 or newer

## Installation

Open PowerShell in the Jiffle folder and install the required packages:

```powershell
python -m pip install flask requests pillow
```

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
- Select an author in the item details to add an `author:name` filter without clearing the current search. The active author and selected media card remain highlighted while results update. Items with multiple authors show each author separately.
- Select an item to inspect its source, dimensions, tags, and available actions.
- Open **Duplicates** and select **Scan** to find similar files.
- Open **Editor** to find images with removable empty margins. Use **Scan library** for a background scan with progress and cancellation, or select an image in **Library** and choose **Open in Editor**.
- Review the original and cropped preview, adjust the Left, Top, Right, and Bottom coordinates, and select **Apply crop**. Jiffle asks for confirmation and retains the original and every accepted version. Use the Versions list to restore an earlier version.
- The crop scan checks static images for almost-uniform white, black, colored, or transparent margins on any side. Animated images and videos are not scanned. **No crop needed** excludes the current version from later scans; **Skip** leaves it pending. Use the status filter and **Reset** to reconsider an earlier decision.
- Configure the detector under **Settings → Crop analysis**: choose the cautious, normal, or sensitive preset, set the minimum removable area and retained padding, and select Local or Vision analysis for an image opened directly in Editor. Library-wide scans always use the local detector.
- Optional Vision analysis is available for one open image at a time. Configure the OpenAI-compatible or Gemini endpoint, model, and API key under **Settings → Crop vision model**, then select **Vision model** in the crop editor. The full original is sent only after this explicit action. An OpenAI-compatible URL may point to a local model server.
- Open **Collections** to create reusable selections and export them.

### Import and review

Open **Import** and drop an image, video, or browser link into the drop area, or paste a supported source URL. The attempt appears in Import history immediately with an **Importing** status while downloading and parsing continue. Its status changes when processing finishes. Repeated imports of the same file or link do not add another item to **Review** when an identical file is already waiting there. Items needing a decision appear in **Review**, where the total and counts by reason are shown. Accept an item to add it to the library, provide a source when requested, or reject it.

By default, previously deleted media is held in **Review** before it can be added again. To block it without asking, open **Settings**, enable **Never re-import deleted media**, and save the settings.

### Configure sources

Open **Settings** to configure source accounts, library display options, storage folders, and tag rules.

## Troubleshooting

- If `python` is not recognized, install Python and enable **Add Python to PATH** during installation.
- If startup reports a missing module, run the installation command again in the same Python environment used to start Jiffle.
- If the page does not open automatically, visit `http://127.0.0.1:5001/` manually.
- If an online import fails, check the source credentials under **Settings** and confirm that the source URL is accessible in a browser.
- If Vision crop analysis fails, verify that the configured model accepts images and returns JSON coordinates. Local crop scanning and manual crop controls remain available without a model.

## License

Jiffle is distributed under the [MIT License](LICENSE).
