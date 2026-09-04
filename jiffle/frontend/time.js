function parseSqliteUtc(value) {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  const text = String(value ?? '').trim();
  if (!text) return null;
  const match = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?$/.exec(text);
  if (!match) return null;
  const date = new Date(`${match[1]}T${match[2]}Z`);
  if (Number.isNaN(date.getTime())) return null;
  const [year, month, day] = match[1].split('-').map(Number);
  const [hour, minute, second] = match[2].split(':').map(Number);
  if (date.getUTCFullYear() !== year || date.getUTCMonth() + 1 !== month ||
      date.getUTCDate() !== day || date.getUTCHours() !== hour ||
      date.getUTCMinutes() !== minute || date.getUTCSeconds() !== second) return null;
  return date;
}

export function formatDateTime(value) {
  const date = parseSqliteUtc(value);
  if (!date) return String(value ?? '');
  try {
    return new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short'}).format(date);
  } catch {
    return String(value ?? '');
  }
}

export function formatDuration(milliseconds) {
  const value = Number(milliseconds);
  if (!Number.isFinite(value) || value < 0) return '';
  let seconds = Math.round(value / 1000);
  const hours = Math.floor(seconds / 3600);
  seconds %= 3600;
  const minutes = Math.floor(seconds / 60);
  seconds %= 60;
  const parts = [];
  if (hours) parts.push(`${hours} h`);
  if (minutes) parts.push(`${minutes} min`);
  if (seconds || !parts.length) parts.push(`${seconds} s`);
  return parts.join(' ');
}

export {parseSqliteUtc};
