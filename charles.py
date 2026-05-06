"""Charles entrypoint. Foundation as of M3 (self-modify capability online). Wakes Telegram channel."""
from channels.telegram import run

if __name__ == "__main__":
    run()
