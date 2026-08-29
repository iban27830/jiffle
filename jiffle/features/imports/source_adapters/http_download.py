from pathlib import Path

import requests


class RequestsMediaDownloader:
    def download(self, url: str, destination: Path, referer: str | None = None) -> None:
        headers = {"User-Agent": "Jiffle/2.0"}
        if referer:
            headers["Referer"] = referer
        with requests.get(url, headers=headers, timeout=30, stream=True) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "text/html" in content_type:
                raise ValueError("Source returned HTML instead of media")
            with destination.open("xb") as file:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        file.write(chunk)
