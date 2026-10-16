"""Interactively create an administrator without placing credentials in shell history."""
from getpass import getpass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.database import SessionLocal
from app.models import User
from app.security import hash_password, normalize_username


def main() -> None:
    try:
        username = normalize_username(input("Administrator username: "))
        password = getpass("Administrator password (12-1024 characters): ")
        confirmation = getpass("Confirm password: ")
        if password != confirmation:
            raise ValueError("The password confirmation does not match.")
        password_hash = hash_password(password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    with SessionLocal() as db:
        try:
            if db.scalar(select(User.id).where(User.username == username)) is not None:
                raise SystemExit("That username already exists.")
            db.add(User(username=username, password_hash=password_hash, is_active=True, is_admin=True))
            db.commit()
        except OperationalError as exc:
            db.rollback()
            raise SystemExit("The users table is unavailable. Apply the database migration before creating an administrator.") from exc
        except IntegrityError as exc:
            db.rollback()
            raise SystemExit("That username already exists.") from exc
    print(f"Administrator '{username}' created.")


if __name__ == "__main__":
    main()
