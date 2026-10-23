"""python -m aitomaton  — run the loop and the HTTP server."""
import logging
import os
import threading

from .config import Config
from .engine import Engine
from .server import serve


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    engine = Engine(Config())
    threading.Thread(target=engine.run_forever, name="loop", daemon=True).start()
    serve(engine)


if __name__ == "__main__":
    main()
