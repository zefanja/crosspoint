"""Admin command line, e.g. inside the container (Portainer → Console):

    python -m app.cli create-admin "Name"
    python -m app.cli create-user "Anna" "Ben" ...
    python -m app.cli reset-code <user-id>
    python -m app.cli list-users
"""

import argparse
import sys

from sqlalchemy import select

from app.auth import create_user, format_code, reset_code
from app.db import get_sessionmaker
from app.models import User


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-admin", help="create an admin and print the code")
    p.add_argument("name")
    p = sub.add_parser("create-user", help="create learners and print their codes")
    p.add_argument("names", nargs="+")
    p = sub.add_parser("reset-code", help="issue a new code (old one stops working)")
    p.add_argument("user_id")
    sub.add_parser("list-users")
    p = sub.add_parser("build-seed", help="(developers) rebuild app/seed/notetypes.apkg")
    p.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "build-seed":
        from app import anki_service

        if anki_service.SEED_PATH.exists() and not args.force:
            print("Seed exists; rebuilding changes notetype ids. Use --force.", file=sys.stderr)
            return 1
        anki_service.SEED_PATH.parent.mkdir(parents=True, exist_ok=True)
        ids = anki_service.build_seed_package(anki_service.SEED_PATH)
        print(f"SEED_IDS = {ids}")
        return 0

    with get_sessionmaker()() as db:
        if args.cmd == "create-admin":
            user, code = create_user(db, args.name, role="admin")
            print(f"{user.display_name}\t{format_code(code)}\t{user.id}")
        elif args.cmd == "create-user":
            for name in args.names:
                user, code = create_user(db, name)
                print(f"{user.display_name}\t{format_code(code)}\t{user.id}")
        elif args.cmd == "reset-code":
            user = db.get(User, args.user_id)
            if user is None:
                print("Unknown user id", file=sys.stderr)
                return 1
            print(f"{user.display_name}\t{format_code(reset_code(db, user))}")
        elif args.cmd == "list-users":
            for user in db.scalars(select(User).order_by(User.display_name)):
                flag = " (deaktiviert)" if user.disabled else ""
                print(f"{user.id}\t{user.role}\t{user.display_name}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
