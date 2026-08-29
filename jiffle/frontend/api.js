export async function api(path, options = {}) {
  const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData;
  const response = await fetch(path, {
    ...options,
    headers: options.body && !isFormData ? {'Content-Type': 'application/json', ...(options.headers || {})} : options.headers,
  });
  if (response.status === 204) return null;
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error?.message || `HTTP ${response.status}`);
  return payload;
}

export async function waitForJob(statusUrl, onProgress) {
  for (;;) {
    const job = await api(statusUrl);
    onProgress?.(job);
    if (job.status === 'completed') return job;
    if (job.status === 'failed') throw new Error(job.error?.message || 'The job failed');
    await new Promise(resolve => setTimeout(resolve, 500));
  }
}
