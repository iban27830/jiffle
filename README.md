# Jiffle

**Turn a folder of images and videos into an organized, reusable media library.**

Import files or source links, review uncertain matches, find what you need, make edits without losing originals, and assemble collections for export. Jiffle runs on your computer or NAS and is controlled in your browser.

```text
Files and links → Import → Review when needed → Library → Editor → Collections → Export
```

## What you can do

- **Import once:** drop files, paste images or links, and import supported post sets. Duplicate checks help avoid repeated copies.
- **Keep the context:** source links, authors, tags, characters, and related versions stay with the media.
- **Find and organize:** search by tags, author, or ID; add personal tags and group related images.
- **Edit without losing originals:** crop images, work with backgrounds where supported, and split videos or animations into fragments.
- **Build collections:** generate a selection, replace individual files, save reusable presets, and export as ZIP or a server folder.
- **Leave and return:** Library links open in a new tab by default. Collection and Editor drafts are kept separately in each browser tab.

## Quick start

### On Windows

Install Python 3.11 or newer. Open PowerShell in the application folder:

```powershell
python -m pip install -r requirements.txt
python .\run.py
```

Open **http://127.0.0.1:5001/** in your browser and keep the terminal running. You can also start Jiffle with `run.bat`. Install FFmpeg if you want video trimming or export conversion. See [installation and updates](guide/setup.md) for the complete steps.

### On a NAS / with Docker

Configure `.env`, choose your media folder and a host port, then run:

```sh
docker compose up -d --build
```

Open `http://<NAS-address>:<HOST_PORT>/` — the supplied example uses port **18001**. Settings and application state are kept in `state/`; media uses the folder you mount. [Docker setup and storage](guide/setup.md#nas--docker) explains the required values. The supplied NAS configuration disables local AI background removal.

## Your first collection

1. In **Settings → Sources**, configure any accounts you want to use.
2. Open **Import** and drop a file or paste a link.
3. If it needs a decision, open **Review** and choose a source or accept your file.
4. Find it in **Library**, inspect its tags, and use **Open in Editor** if necessary.
5. Open **Collections → Create**, enter tags and a count, then **Generate**. Replace unwanted items and **Save collection**.

## User guide

| I want to… | Read |
|---|---|
| Install, start, update, or move my data safely | [Setup](guide/setup.md) |
| Import files and links, understand duplicates, or resolve Review | [Import and Review](guide/import-review.md) |
| Search, tag, and organize my library | [Library](guide/library.md) |
| Crop, replace a background, or trim a clip | [Editor](guide/editor.md) |
| Build a collection, resume a draft, and export | [Collections](guide/collections.md) |
| Configure sources, crop checks, display, and export rules | [Settings](guide/settings.md) |
| Understand an error or recover a workflow | [Troubleshooting](guide/troubleshooting.md) |

## Your files stay yours

Edits retain the original and version history; export conversions do not overwrite library files. Back up your media and application data before major updates or moving folders. Unsaved browser drafts are a convenience, not a backup. Use Jiffle on a trusted local network, not as an internet-facing service.

## License

[MIT](LICENSE).
