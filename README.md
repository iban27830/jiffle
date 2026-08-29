# Jiffle

Jiffle is a local application for importing, organizing, tagging, and curating images and videos.

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
- Open **Collections** to create reusable selections and export them.

### Import and review

Open **Import** and drop an image, video, or browser link into the drop area, or paste a supported source URL. The attempt appears in Import history immediately with an **Importing** status while downloading and parsing continue. Its status changes when processing finishes. Repeated imports of the same file or link do not add another item to **Review** when an identical file is already waiting there. Items needing a decision appear in **Review**, where the total and counts by reason are shown. Accept an item to add it to the library, provide a source when requested, or reject it.

By default, previously deleted media is held in **Review** before it can be added again. To block it without asking, open **Settings**, enable **Never re-import deleted media**, and save the settings.

### Configure sources

Open **Settings** to configure source accounts, library display options, storage folders, and tag rules.

For AI tags, choose the API format, enter the provider URL, model, API key, and tagging prompt, then use **Test AI** before saving.

## Troubleshooting

- If `python` is not recognized, install Python and enable **Add Python to PATH** during installation.
- If startup reports a missing module, run the installation command again in the same Python environment used to start Jiffle.
- If the page does not open automatically, visit `http://127.0.0.1:5001/` manually.
- If an online import fails, check the source credentials under **Settings** and confirm that the source URL is accessible in a browser.

## License

Jiffle is distributed under the [MIT License](LICENSE).
