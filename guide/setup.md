# Install, start, and update

[Home](../README.md) · [Troubleshooting](troubleshooting.md)

## Windows

You need Python 3.11 or newer. Enable **Add Python to PATH** during installation.

1. Extract the application into a folder you can write to.
2. Open PowerShell in that folder and run:

   ```powershell
   python -m pip install -r requirements.txt
   python .\run.py
   ```

3. Open **http://127.0.0.1:5001/**. Jiffle does not launch a browser itself.
4. Keep the terminal open while using Jiffle. Press **Ctrl+C** there to stop it.

For later launches, use the same command or double-click `run.bat`. A normal Windows start checks for missing basic packages and may install them; the first setup therefore needs internet access. You can disable automatic installation with `JIFFLE_NO_AUTO_INSTALL=1` after installing requirements yourself.

### Video tools

FFmpeg is needed for trimming and export conversion. One Windows installation option is:

```powershell
winget install Gyan.FFmpeg
```

Restart PowerShell after installation, then start Jiffle again. Image import, search, and ordinary cropping do not require a GPU.

### Choose another port

```powershell
$env:PORT='18001'
python .\run.py
```

Then open `http://127.0.0.1:18001/`. By default Jiffle listens on all network interfaces; use it only on a trusted network.

## NAS / Docker

Docker includes Python dependencies and FFmpeg. In the application folder:

1. Copy `.env.example` to `.env`.
2. Set these values in `.env`:

   | Value | What to enter |
   |---|---|
   | `HOST_PORT` | An unused host port from 18000–18199; the example uses 18001 |
   | `MEDIA_HOST_PATH` | The existing media folder on your NAS; it must be writable |
   | `HOME_HOST` | The NAS address used by the dashboard link |
   | `MEM_LIMIT` | The container memory limit; the example uses `1g` |
   | `SERVICE_DESCRIPTION` | A short dashboard description |

3. Run `docker compose up -d --build`.
4. Open `http://<NAS-address>:<HOST_PORT>/` from your browser.

Use the exact case of folder names on the NAS. The media mount is read-write because imports and edits create files there. Inside **Settings → Storage**, use container paths: the supplied configuration mounts media at `/media/pics` and application data at `/data`. A Windows drive path is not a path inside the container.

The supplied configuration keeps settings, database, thumbnails, import staging, and exports in persistent `state/`. Media stays in the mounted media folder. Rebuilding the application must not remove either folder.

**Local AI background removal is disabled in the NAS configuration.** Do not install large AI runtimes on the NAS to bypass that restriction. Crop review, library management, and the other available workflows remain usable.

## Updating

Finish or stop long-running imports, scans, exports, and edits first; restarting interrupts active work.

- **Windows:** stop Jiffle, run `update.bat`, and let it download the application update and restart. It keeps your media and settings and makes a database backup before replacing application files. Keep that backup until you have checked the updated library.
- **Docker:** update the application files, then run `docker compose up -d --build` again. Keep `.env`, `state/`, and your media folder. If your NAS uses a service deployment tool, use its normal rebuild-and-restart action instead.

Data upgrades happen automatically at startup. If an update fails, stop rather than deleting or recreating your library; see [troubleshooting](troubleshooting.md).

## What to back up

For a standard Windows installation, keep `jiffle-data/`, the local `settings.json`, your media folder, and any pending `import-staging/` files. For Docker, keep `state/`, `.env`, and the mounted media folder. Include custom folders you selected in Settings. Stop Jiffle before making a simple file-copy backup.

Browser drafts live in the current tab and are not included in a server backup. Save important collections before clearing browser data or closing your session.
