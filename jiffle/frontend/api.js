const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

export async function api(path, options = {}) {
  const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData;
  const method = String(options.method || 'GET').toUpperCase();
  const maxAttempts = method === 'GET' ? 3 : 1;
  let response;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      response = await fetch(path, {
        ...options,
        headers: options.body && !isFormData ? {'Content-Type': 'application/json', ...(options.headers || {})} : options.headers,
      });
      break;
    } catch (error) {
      if (attempt >= maxAttempts) throw error;
      await sleep(200 * attempt);
    }
  }
  if (response.status === 204) return null;
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error?.message || `HTTP ${response.status}`);
  return payload;
}

const activeJobWaiters = new Map();
let batchPollerRunning = false;
const JOB_INACTIVITY_TIMEOUT_MS = 180000;
const MAX_CONSECUTIVE_POLL_FAILURES = 5;

function settleJobWaiter(entry, job) {
  entry.pollFailures = 0;
  const progressKey = `${job.status}:${job.progress ?? ''}:${job.message ?? ''}:${job.processed ?? ''}`;
  if (entry.lastProgressKey !== progressKey) {
    entry.lastProgressKey = progressKey;
    entry.lastActivityAt = Date.now();
  }
  for (const waiter of entry.waiters) {
    try { waiter.onProgress?.(job); } catch {}
  }
  if (job.status === 'completed') {
    activeJobWaiters.delete(entry.jobId);
    for (const waiter of entry.waiters) waiter.resolve(job);
    return true;
  }
  if (job.status === 'failed') {
    activeJobWaiters.delete(entry.jobId);
    const err = new Error(job.error?.message || 'The job failed');
    for (const waiter of entry.waiters) waiter.reject(err);
    return true;
  }
  if (job.status === 'cancelled' || job.status === 'canceled') {
    activeJobWaiters.delete(entry.jobId);
    const err = new Error(job.error?.message || job.message || 'The job was cancelled');
    for (const waiter of entry.waiters) waiter.reject(err);
    return true;
  }
  if (
    job.type !== 'source_set_import'
    && entry.lastActivityAt
    && (Date.now() - entry.lastActivityAt) > JOB_INACTIVITY_TIMEOUT_MS
  ) {
    activeJobWaiters.delete(entry.jobId);
    const err = new Error('Background job timed out while resolving providers.');
    for (const waiter of entry.waiters) waiter.reject(err);
    return true;
  }
  return false;
}

function handlePollError(entry, error) {
  entry.pollFailures = (entry.pollFailures || 0) + 1;
  if (entry.pollFailures >= MAX_CONSECUTIVE_POLL_FAILURES) {
    activeJobWaiters.delete(entry.jobId);
    for (const waiter of entry.waiters) waiter.reject(error);
  }
}

async function runCoalescedJobPoller() {
  if (batchPollerRunning) return;
  batchPollerRunning = true;
  try {
    while (activeJobWaiters.size > 0) {
      const entries = [...activeJobWaiters.values()];
      if (entries.length === 1) {
        const entry = entries[0];
        try {
          const job = await api(`/api/v1/jobs/${entry.jobId}`);
          settleJobWaiter(entry, job);
        } catch (error) {
          if (/404|not found/i.test(String(error?.message || ''))) {
            activeJobWaiters.delete(entry.jobId);
            for (const waiter of entry.waiters) waiter.reject(error);
          } else {
            handlePollError(entry, error);
          }
        }
      } else {
        for (let i = 0; i < entries.length; i += 100) {
          const chunk = entries.slice(i, i + 100);
          const idsParam = chunk.map(e => e.jobId).join(',');
          try {
            const data = await api(`/api/v1/jobs?ids=${idsParam}`);
            const items = Array.isArray(data?.items) ? data.items : [];
            const seen = new Set();
            for (const job of items) {
              const jid = Number(job.id);
              seen.add(jid);
              const entry = activeJobWaiters.get(jid);
              if (entry) settleJobWaiter(entry, job);
            }
            for (const entry of chunk) {
              if (!seen.has(entry.jobId) && activeJobWaiters.has(entry.jobId)) {
                activeJobWaiters.delete(entry.jobId);
                const err = new Error('Background job was not found.');
                for (const waiter of entry.waiters) waiter.reject(err);
              }
            }
          } catch (error) {
            for (const entry of chunk) {
              handlePollError(entry, error);
            }
          }
        }
      }
      if (activeJobWaiters.size > 0) {
        await sleep(600);
      }
    }
  } finally {
    batchPollerRunning = false;
    if (activeJobWaiters.size > 0) {
      runCoalescedJobPoller();
    }
  }
}

export async function waitForJob(statusUrl, onProgress) {
  const match = String(statusUrl ?? '').match(/^\/api\/v1\/jobs\/(\d+)$/);
  if (match) {
    const jobId = Number(match[1]);
    return new Promise((resolve, reject) => {
      let entry = activeJobWaiters.get(jobId);
      if (!entry) {
        entry = {jobId, waiters: [], lastActivityAt: Date.now(), lastProgressKey: '', pollFailures: 0};
        activeJobWaiters.set(jobId, entry);
      }
      entry.waiters.push({onProgress, resolve, reject});
      runCoalescedJobPoller();
    });
  }
  for (;;) {
    const job = await api(statusUrl);
    onProgress?.(job);
    if (job.status === 'completed') return job;
    if (job.status === 'failed') throw new Error(job.error?.message || 'The job failed');
    if (job.status === 'cancelled' || job.status === 'canceled') throw new Error(job.error?.message || job.message || 'The job was cancelled');
    await sleep(500);
  }
}
