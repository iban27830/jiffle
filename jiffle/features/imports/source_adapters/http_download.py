from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import time

import requests


class MediaDownloadError(requests.RequestException):
    """A download failure with a stable code suitable for import diagnostics."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RequestsMediaDownloader:
    """Stream original media with bounded retries for transient CDN failures."""

    connect_timeout = 15
    read_timeout = 120
    max_attempts = 3
    transient_statuses = {408, 429, 500, 502, 503, 504}

    def download(self, url: str, destination: Path, referer: str | None = None) -> None:
        headers = {"User-Agent": "Jiffle/2.0"}
        if referer:
            headers["Referer"] = referer
        for attempt in range(self.max_attempts):
            destination.unlink(missing_ok=True)
            response = None
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=(self.connect_timeout, self.read_timeout),
                    stream=True,
                )
                status = getattr(response, "status_code", None)
                content_type = str((getattr(response, "headers", None) or {}).get("Content-Type", ""))
                if "text/html" in content_type.lower() and status not in {401, 403, 404}:
                    raise ValueError("Source returned HTML instead of media")
                if status in self.transient_statuses:
                    message = f"Source returned temporary HTTP {status}."
                    if attempt < self.max_attempts - 1:
                        _sleep_backoff(response, attempt)
                        continue
                    if status == 408:
                        code = "import.download_timeout"
                    elif status == 429:
                        code = "import.download_rate_limited"
                    else:
                        code = "import.download_unavailable"
                    raise MediaDownloadError(code, message)
                if status in {401, 403, 404}:
                    raise MediaDownloadError(
                        "import.source_media_unavailable",
                        f"Source media is unavailable (HTTP {status}).",
                    )
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                if "text/html" in content_type.lower():
                    raise ValueError("Source returned HTML instead of media")
                with destination.open("wb") as file:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            file.write(chunk)
                return
            except MediaDownloadError:
                destination.unlink(missing_ok=True)
                raise
            except ValueError:
                destination.unlink(missing_ok=True)
                raise
            except (requests.Timeout, requests.ConnectionError) as error:
                destination.unlink(missing_ok=True)
                if attempt < self.max_attempts - 1:
                    _sleep_backoff(None, attempt)
                    continue
                code = "import.download_timeout" if isinstance(error, requests.Timeout) else "import.download_unavailable"
                message = "Media download timed out." if code.endswith("timeout") else "Media download failed because the source was unavailable."
                raise MediaDownloadError(code, message) from error
            except requests.RequestException as error:
                destination.unlink(missing_ok=True)
                error_response = getattr(error, "response", None)
                error_status = getattr(error_response, "status_code", None)
                if error_status in self.transient_statuses and attempt < self.max_attempts - 1:
                    _sleep_backoff(error_response, attempt)
                    continue
                raise MediaDownloadError("import.download_unavailable", "Media download failed.") from error
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()


def _sleep_backoff(response, attempt: int) -> None:
    value = (getattr(response, "headers", None) or {}).get("Retry-After") if response is not None else None
    delay = 0.0
    if value:
        try:
            delay = max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                delay = 0.0
    if delay <= 0:
        delay = min(5.0, 0.5 * (2 ** attempt))
    time.sleep(delay)
