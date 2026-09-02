// Pure, browser-side helpers for the responsive background editor preview.

export const MAX_PREVIEW_SIDE = 2048;

export function preserveGamma(preserve = 0) {
  const value = Math.max(0, Math.min(100, Number(preserve) || 0));
  return 1 - (value * 0.006);
}

export function alphaChannelStats(data) {
  let visiblePixels = 0;
  let softEdgePixels = 0;
  for (let index = 3; index < data.length; index += 4) {
    const alpha = data[index];
    if (alpha > 0) visiblePixels += 1;
    if (alpha > 0 && alpha < 255) softEdgePixels += 1;
  }
  return {
    visiblePixels,
    softEdgePixels,
    hasSoftEdges: softEdgePixels > 0,
  };
}

export function transformAlphaChannel(data, preserve = 0) {
  const gamma = preserveGamma(preserve);
  for (let index = 3; index < data.length; index += 4) {
    const level = data[index] / 255;
    data[index] = Math.max(0, Math.min(255, Math.round((level ** gamma) * 255)));
  }
  return data;
}

export function previewDimensions(width, height, maxSide = MAX_PREVIEW_SIDE) {
  const sourceWidth = Math.max(1, Number(width) || 1);
  const sourceHeight = Math.max(1, Number(height) || 1);
  const limit = Math.max(1, Number(maxSide) || MAX_PREVIEW_SIDE);
  const scale = Math.min(1, limit / Math.max(sourceWidth, sourceHeight));
  return {
    width: Math.max(1, Math.round(sourceWidth * scale)),
    height: Math.max(1, Math.round(sourceHeight * scale)),
    scale,
  };
}

// ImageOps.fit uses a centered crop after scaling until both dimensions cover
// the destination. Returning source and destination rectangles keeps the
// canvas implementation in lockstep with the server composition.
export function calculateCoverPlacement(sourceWidth, sourceHeight, targetWidth, targetHeight) {
  const sourceW = Math.max(1, Number(sourceWidth) || 1);
  const sourceH = Math.max(1, Number(sourceHeight) || 1);
  const targetW = Math.max(1, Number(targetWidth) || 1);
  const targetH = Math.max(1, Number(targetHeight) || 1);
  const scale = Math.max(targetW / sourceW, targetH / sourceH);
  const fittedW = sourceW * scale;
  const fittedH = sourceH * scale;
  return {
    sx: Math.max(0, (fittedW - targetW) / (2 * scale)),
    sy: Math.max(0, (fittedH - targetH) / (2 * scale)),
    sWidth: targetW / scale,
    sHeight: targetH / scale,
    dx: 0,
    dy: 0,
    dWidth: targetW,
    dHeight: targetH,
  };
}

export function prepareBlurParameters(blur = 0, scale = 1) {
  const value = Math.max(0, Math.min(100, Number(blur) || 0));
  // The server uses GaussianBlur(blur / 5). CSS canvas filters use CSS pixels,
  // so compensate for a downscaled preview before drawing.
  const radius = (value / 5) * Math.max(0.01, Number(scale) || 1);
  return { value, radius, cssFilter: radius > 0 ? `blur(${radius}px)` : 'none' };
}

export function drawForegroundPreview(context, image, preserve = 0, width = image?.naturalWidth, height = image?.naturalHeight) {
  if (!context || !image) return null;
  const dimensions = previewDimensions(width || image.naturalWidth, height || image.naturalHeight);
  context.canvas.width = dimensions.width;
  context.canvas.height = dimensions.height;
  context.canvas.dataset.previewScale = String(dimensions.scale);
  context.clearRect(0, 0, dimensions.width, dimensions.height);
  context.drawImage(image, 0, 0, dimensions.width, dimensions.height);
  const pixels = context.getImageData(0, 0, dimensions.width, dimensions.height);
  const alphaStats = alphaChannelStats(pixels.data);
  transformAlphaChannel(pixels.data, preserve);
  context.putImageData(pixels, 0, 0);
  return {...dimensions, ...alphaStats};
}

export function drawCompositionPreview(context, foregroundCanvas, backgroundImage, blur = 0) {
  if (!context || !foregroundCanvas || !backgroundImage) return false;
  const {width, height} = foregroundCanvas;
  context.canvas.width = width;
  context.canvas.height = height;
  context.clearRect(0, 0, width, height);
  const placement = calculateCoverPlacement(
    backgroundImage.naturalWidth || backgroundImage.width,
    backgroundImage.naturalHeight || backgroundImage.height,
    width,
    height,
  );
  const blurParameters = prepareBlurParameters(blur, Number(foregroundCanvas.dataset.previewScale) || 1);
  context.save();
  context.filter = blurParameters.cssFilter;
  context.drawImage(backgroundImage, placement.sx, placement.sy, placement.sWidth, placement.sHeight, placement.dx, placement.dy, placement.dWidth, placement.dHeight);
  context.restore();
  context.drawImage(foregroundCanvas, 0, 0);
  return true;
}
