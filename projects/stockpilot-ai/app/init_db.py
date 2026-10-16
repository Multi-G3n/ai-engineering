from alembic import command
from alembic.config import Config
from pathlib import Path


def main():
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    command.upgrade(config, "head")
    print("StockPilot database upgraded to the latest version.")


if __name__ == "__main__":
    main()
