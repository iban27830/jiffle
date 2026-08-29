import os
from threading import Timer
import webbrowser

from jiffle import create_app

app = create_app()

if __name__ == "__main__":
    if not os.environ.get("JIFFLE_NO_BROWSER"):
        Timer(1, lambda: webbrowser.open_new("http://127.0.0.1:5001/")).start()
    app.run(host="127.0.0.1", port=5001, debug=False)
