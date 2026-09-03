"""Start the Jiffle web application.

On the first run the launcher checks for the Python packages the application
requires and installs any missing ones through pip before starting the server.
"""

import importlib.util
import os
import subprocess
import sys
from threading import Timer
import webbrowser


# Module import names mapped to the pip packages that provide them. These
# cover everything Jiffle needs to start and to serve its core workflows.
# The optional background-removal runtime (PyTorch and friends) installs
# itself lazily the first time a model actually runs.
STARTUP_PACKAGES = (
    ("flask", "flask"),
    ("PIL", "pillow"),
    ("requests", "requests"),
    ("imagehash", "imagehash"),
    ("cv2", "opencv-python"),
)


def missing_startup_packages() -> list[str]:
    """Return pip package names for required modules that are not installed."""
    return [
        package
        for module, package in STARTUP_PACKAGES
        if importlib.util.find_spec(module) is None
    ]


def ensure_startup_packages() -> None:
    """Install required packages that are missing, then fail loudly if any of
    them still cannot be imported."""
    missing = missing_startup_packages()
    if not missing:
        return
    print(
        "Jiffle is missing Python packages it needs to run:\n  "
        + ", ".join(missing)
    )
    print("Installing them now. This requires an internet connection...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            "Jiffle could not install the missing packages automatically. "
            "Check your internet connection and Python permissions, then try:\n"
            f"  python -m pip install {' '.join(missing)}"
        ) from error
    importlib.invalidate_caches()
    still_missing = missing_startup_packages()
    if still_missing:
        raise SystemExit(
            "Jiffle could not install these required packages: "
            + ", ".join(still_missing)
            + ".\nTry installing them manually:\n"
            f"  python -m pip install {' '.join(still_missing)}"
        )


def main() -> None:
    ensure_startup_packages()

    from jiffle import create_app

    app = create_app()

    if not os.environ.get("JIFFLE_NO_BROWSER"):
        Timer(1, lambda: webbrowser.open_new("http://127.0.0.1:5001/")).start()
    app.run(host="127.0.0.1", port=5001, debug=False)


if __name__ == "__main__":
    main()
