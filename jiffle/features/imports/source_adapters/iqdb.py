"""Optional IQDB reverse-search adapter used after exact lookup fails."""

from pathlib import Path
import re
from urllib.parse import urljoin

import requests


class IqdbReverseSearch:
    endpoint = "https://iqdb.org/"

    def search_similar(self, image_path: Path) -> list[dict[str, object]]:
        handle = None
        try:
            handle = Path(image_path).open("rb")
            response = requests.post(
                self.endpoint,
                files={"file": (Path(image_path).name, handle, "application/octet-stream")},
                headers={"User-Agent": "Jiffle/2.0"}, timeout=5,
            )
            response.raise_for_status()
            html = response.text
        except (OSError, requests.RequestException):
            return []
        finally:
            if handle is not None:
                handle.close()
        results = []
        patterns = [
            r"(?:data-similarity|similarity)[^>]*[=:]\s*['\"]?(\d+(?:\.\d+)?)",
            r"class=['\"][^'\"]*similarity[^'\"]*['\"][^>]*>\s*(\d+(?:\.\d+)?)%?",
        ]
        matches = []
        for pattern in patterns:
            matches.extend(re.finditer(pattern, html, re.I))
        for match in matches:
            try:
                confidence = float(match.group(1))
            except ValueError:
                continue
            if confidence <= 1:
                confidence *= 100
            anchor = html[max(0, match.start() - 500):match.end() + 500]
            href = re.search(r"href=['\"]([^'\"]+)", anchor, re.I)
            if not href:
                continue
            canonical = urljoin(self.endpoint, href.group(1))
            image = re.search(r"<img[^>]+src=['\"]([^'\"]+)", anchor, re.I)
            results.append({
                "provider": "iqdb", "canonical_url": canonical,
                "preview_url": urljoin(self.endpoint, image.group(1)) if image else None,
                "match_method": "perceptual",
            })
        return results
