from app.database import Base, engine
from app import models  # noqa: F401 - register mapped tables


def main():
    Base.metadata.create_all(bind=engine)
    print("StockPilot database initialized.")


if __name__ == "__main__":
    main()
