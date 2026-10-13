# Crop, backgrounds, and trimming

[Home](../README.md) · [Settings](settings.md) · [Troubleshooting](troubleshooting.md)

## Open the right tool

Select a file in **Library → Open in Editor**:

- A static image opens **Crop** the first time; use the **Crop / Background** tabs to switch. Jiffle remembers the tool for that file and version.
- A video or animation opens **Trim**. Image-only tools are not offered for it.
- **Replace background** explicitly opens Background.

The sidebar's Editor badges show separate queues: amber **Crop**, purple **Background**, and blue **Videos & animations**. Each badge has its own icon and label and opens its queue. Counts refer to the full queue, not only the displayed page.

Drafts preserve tool parameters in the current browser tab. Switching a tool or revisiting a file does not automatically run analysis, create a mask, or save fragments. A different source version has a separate draft; old coordinates and masks must not be applied to it.

## Crop an image

1. Open the image in **Library → Open in Editor**. The crop editor starts from the whole image, so you can crop any static image even when the detector finds no margins; a queued image with a suggestion opens with that proposal instead.
2. Compare the source and preview. Drag the crop rectangle or its resize handles directly on the source image, or adjust **Left**, **Top**, **Right**, and **Bottom** numerically, and use **Zoom** for a closer look.
3. Choose:

   | Action | Result |
   |---|---|
   | **Apply crop** | Create and activate a new version; keep the source in Versions |
   | **No crop needed** | Explicitly approve this version without changing its pixels |
   | **Analyze** | Run the local detector and use its proposal when it finds one |
   | **Reset suggested crop** / **Reset to full image** | Reset the coordinate fields to the suggestion, or to the whole image in a manual session |
   | **Reset to original** | Restore the original image version while keeping saved versions in history |
   | **Skip** | Leave a queued decision unresolved; it remains in Needs attention |

The local detector looks for near-uniform or transparent margins, but cropping does not depend on it: a manual session starts from the whole image so you can always pick the area yourself. **Find crop candidates** scans the existing library on request. Repeating a scan can reuse previous results. **Reopen review** lets you reconsider an earlier decision.

Configure sensitivity, minimum removable area, and padding in **Settings → Crop analysis**. Optional **Vision model** analysis sends the current image to the endpoint you configured, only after your explicit request; normal scans and automatic import checks use the local detector.

### Keep unchecked crops out of collections

Enable **Check crop before using new images in collections** in **Settings → Import and limits**, then **Save settings**. It is off by default on a new installation.

- Newly accepted static images are checked; existing unchecked images are not scanned automatically.
- Already-known unresolved crop candidates count immediately.
- A needed crop, pending check, failed check, or skipped decision blocks selection, manual addition, and export for that active version.
- **Apply crop** or **No crop needed** clears the relevant decision. Restoring an original that still needs crop review blocks that original again.
- A failed check does not delete the imported file. Retry **Analyze current**, or inspect the image and explicitly choose **No crop needed**.
- Automatic checking is limited to 16-megapixel images. Larger images remain available for manual review rather than being silently approved.

Turning the option off lifts the restriction without erasing review decisions. Saved collections are not rewritten when a member becomes blocked; export tells you which IDs need attention.

## Replace a background

Local mask generation is disabled in the supplied NAS configuration. The following generation steps are for an installation where background processing is available, such as a capable Windows computer. Merely opening Background does not start it.

1. Open **Background** for a static image. **Find background candidates** in the queue or **Check candidate** for one image can suggest suitable files.
2. Choose a processing device and removal model in **Settings → Background removal**. Automatic, CPU, and CUDA choices are available; CUDA requires a compatible local installation. **Test access** checks model access. Public BiRefNet choices do not need a token; RMBG-2.0 may require an authorized Hugging Face token.
3. Select **Remove background / Preview**. First use may install the runtime and download model files; allow time and disk space for setup.
4. Inspect **Cutout**, **Mask**, and **Zoom**. A completed preview is cached for its source version and processing settings.
5. Select **Choose background**. Pick a category, select a background, or import one. **Select background** confirms the choice; Cancel, Escape, or closing the modal preserves the previous choice.
6. Adjust **Blur**, review the composition, and select **Apply background**. This creates a new PNG version.

**Regenerate automatic mask** deliberately reruns processing. You do not need to use it just because you revisited the editor. If a preview is stale, return to its source version or generate a fresh one.

## Trim a video or animation

1. Open the clip in **Trim**.
2. Set **Start** and **End**, accepting seconds or `mm:ss.d`. For video, drag timeline markers or use **Set start here / Set end here**.
3. Select **Add segment** and repeat. Segments must not overlap and must be at least 0.2 seconds long.
4. Preview the marked range. Use the wheel or zoom controls for precision; **Fit all**, **Fit selection**, arrows, or Shift-drag adjust the visible timeline.
5. Select **Save fragments** and wait for each row to become **Saved**. Encoding requires FFmpeg.

Fragments keep the source metadata and appear as separate related library items. The original stays in Library; automatic collection selection uses its fragments instead. Opening a fragment in Editor returns to the source's segments so you can revise the split.

If a pending clip needs no changes, use **Keep original, no trim** in the editor or **Looks good** in the queue. **Move to end** defers a queue decision. Trim review and blocking unreviewed exports are separate [settings](settings.md).

## Restore a version

Use **Versions → Restore** or **Reset to original**. Restoring changes the active version; it does not erase later versions. Check crop eligibility again if you return to an unresolved original. Keep important completed edits on the server rather than relying only on an unsaved browser draft.
