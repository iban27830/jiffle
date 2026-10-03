const MIN_SEGMENT_MS = 200;

export function secondsToMs(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.round(number * 1000)) : 0;
}

export function msToSeconds(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, number) / 1000 : 0;
}

export function segmentDurationMs(segment) {
  return Math.max(0, Number(segment?.end_ms || 0) - Number(segment?.start_ms || 0));
}

export function sortSegments(segments) {
  return [...(segments || [])].sort(
    (left, right) => Number(left.start_ms) - Number(right.start_ms),
  );
}

export function validateSegment(segments, startMs, endMs, durationMs) {
  const start = Math.round(Number(startMs));
  const end = Math.round(Number(endMs));
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    return {ok: false, message: 'The segment end must be after its start.'};
  }
  if (end - start < MIN_SEGMENT_MS) {
    return {ok: false, message: 'Each segment must last at least 0.2 s.'};
  }
  if (durationMs && end > Number(durationMs) + 1000) {
    return {ok: false, message: 'The segment is past the end of the clip.'};
  }
  for (const segment of segments || []) {
    if (start < Number(segment.end_ms) && end > Number(segment.start_ms)) {
      return {ok: false, message: 'Segments must not overlap.'};
    }
  }
  return {ok: true, message: ''};
}

export function segmentsPayload(segments) {
  return sortSegments(segments).map(segment => ({
    start_ms: Math.round(Number(segment.start_ms)),
    end_ms: Math.round(Number(segment.end_ms)),
  }));
}

export function describeSegment(segment, index) {
  return `Part ${index}: ${msToSeconds(segment.start_ms).toFixed(2)}s - ${msToSeconds(segment.end_ms).toFixed(2)}s`;
}
