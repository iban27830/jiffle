# Troubleshooting

[Home](../README.md) · [Setup](setup.md) · [Settings](settings.md)

## The interface will not open

Jiffle does not open a browser automatically. Keep the server running and open the address yourself: `http://127.0.0.1:5001/` for a default Windows launch, or the NAS address and configured host port for Docker. Check the terminal for a startup error or a port already in use.

If `python` is not recognized, install Python and enable **Add Python to PATH**. For missing basic packages, run `python -m pip install -r requirements.txt` in the same environment you use to start Jiffle.

## My draft seems missing

Return to the **same browser tab**. The Collections sidebar restores its last screen; **Create** from the list resumes a stored builder draft. Do not select **New draft** unless you want to discard it. Browser clearing, a private session ending, or losing the tab session may remove unsaved work.

A warning on a restored selection means a file changed eligibility or was deleted. Its ID is preserved instead of being silently replaced. Resolve it or use **Replace**.

## Import says not found, unavailable, or not configured

Expand Import history's diagnostics or the Review card's log. Check **Settings → Sources → Test** and open the source link in your browser.

- **Not configured / requires authorization:** supply or renew credentials.
- **Unavailable:** retry later or check network access to that provider.
- **Not found:** that search answered but did not find an acceptable match.
- **Candidate found but download failed:** the post exists, but its file could not be verified or downloaded.
- **Not supported:** the source does not offer that search method; it is not a credential error.

Recheck pending Review items after fixing the issue. A Pawchive **preview only** post may not provide a downloadable original. When you have a valid local file but no source, inspect it and use **Accept without choosing a source**.

## A crop check failed or export is blocked

Open the listed media ID in **Editor → Crop**. Retry **Analyze current**, adjust and apply a crop, or inspect the image and explicitly select **No crop needed**. **Skip** leaves it unresolved. Larger than 16-megapixel images require manual attention instead of automatic import approval.

The file stays in Library after a failed check. A saved collection also remains intact. Restoring an unresolved original can block it again even when a different cropped version was approved.

For Trim-related export blocking, confirm **Keep original, no trim / Looks good** or finish saving the intended fragments.

## Video trimming or export conversion fails

Install FFmpeg and restart the Windows terminal so it is in `PATH`; Docker already includes it. Check the error for the particular file. Increase the per-file export limit if the requested conversion cannot fit, or change the conversion rule. This does not change the original stored file.

## Background processing is unavailable

The supplied NAS service intentionally disables local AI mask generation. On a supported Windows installation, check the selected device and model in **Settings → Background removal**. If CUDA is unavailable, use Automatic or CPU, or install a compatible GPU runtime before choosing CUDA.

First use needs internet access and space for the runtime and model cache. **Test access** helps distinguish an invalid token, missing model permission, and network failure. A stale preview belongs to a different source version or processing configuration; restore that version or generate a new preview.

## Vision analysis fails

Check that the endpoint, provider format, model, and key are correct and the model accepts images. Local analysis remains available without Vision. Do not paste credentials into public error reports.

## An update or data upgrade fails

Stop Jiffle and preserve the current data and backup folders. Do not delete the database or start with a blank library as a workaround. Read the startup error and return to a known-good application/data backup if needed. For future updates, finish active tasks first and keep the backup until the library has been checked.
