"""First start: create an admin if none exists and print the code ONCE to the log.

This is the only place where a login code may be written to the log (SPEC §8).
Run by docker/entrypoint.sh after the migrations: `python -m app.bootstrap`.
"""

import sys

from app.auth import count_admins, create_user, format_code
from app.config import get_settings
from app.db import get_sessionmaker


def ensure_initial_admin() -> str | None:
    """Returns the plain admin code if an admin was created, else None."""
    with get_sessionmaker()() as db:
        if count_admins(db) > 0:
            return None
        _, code = create_user(db, get_settings().initial_admin_name, role="admin")
        return code


def main() -> int:
    code = ensure_initial_admin()
    if code:
        line = "=" * 64
        print(
            f"\n{line}\n  First start: admin '{get_settings().initial_admin_name}' created.\n"
            f"  Admin code (shown only this once):\n\n"
            f"      {format_code(code)}\n\n"
            f"  Login: {get_settings().base_url}/login\n{line}\n",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
