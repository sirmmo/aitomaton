"""python -m aitomaton                  run the loop and the HTTP server
   python -m aitomaton telegram-chats   list the chats that wrote to the bot (to find AITOMATON_TELEGRAM_CHAT)
   python -m aitomaton notify-test      send a test notification to every configured channel"""
import logging
import os
import sys
import threading

from .config import Config
from .engine import Engine
from .server import serve


def tool(cmd):
    from .notify import Notifier
    n = Notifier(Config())
    if cmd == "telegram-chats":
        if not n.token:
            sys.exit("set AITOMATON_TELEGRAM_TOKEN first, then write to the bot (or add it to a group)")
        if n.cfg.telegram_intake != "off":
            print("note: with the intake on, the running bot takes the updates first; it logs unknown chats and "
                  "users, and answers them with their id")
        chats = n.telegram_chats()
        for chat, name in chats.items():
            print("%s\t%s" % (chat, name))
        if not chats:
            print("no chats yet: write to the bot, or add it to a group and post there, then run this again")
    elif cmd == "notify-test":
        if not n.configured():
            sys.exit("no channel: set AITOMATON_TELEGRAM_TOKEN and AITOMATON_TELEGRAM_CHAT, or AITOMATON_NOTIFY_URL")
        event = n.cfg.notify_events[0] if n.cfg.notify_events else "emergency"
        print(n.send(event, "aitomaton: test notification. Emergencies and fixes will be posted here."))
    else:
        sys.exit(__doc__)


def main():
    if len(sys.argv) > 1:
        return tool(sys.argv[1])
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    engine = Engine(Config())
    threading.Thread(target=engine.run_forever, name="loop", daemon=True).start()
    if engine.cfg.telegram_intake != "off":
        if engine.notifier.token:
            from .telegram import TelegramBot
            threading.Thread(target=TelegramBot(engine).run_forever, name="telegram", daemon=True).start()
        else:
            logging.getLogger("aitomaton").warning("AITOMATON_TELEGRAM_INTAKE is on but there is no bot token")
    serve(engine)


if __name__ == "__main__":
    main()
