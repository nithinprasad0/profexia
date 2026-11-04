"""Promote an existing user to the admin role.

Public registration cannot create admins. After registering normally, run:

    python -m app.infrastructure.create_admin --email you@example.com

(locally: `docker compose exec api python -m app.infrastructure.create_admin --email ...`;
on Render: from the service Shell tab).
"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.database import SessionLocal, engine
from app.domain.models import Role, User


async def promote(email: str) -> int:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"No user with email {email!r}. Register the account first.", file=sys.stderr)
            return 1
        role = await session.scalar(select(Role).where(Role.name == "admin"))
        if role is None:
            print("Admin role missing. Run `python -m app.infrastructure.seed` first.", file=sys.stderr)
            return 1
        user.role_id = role.id
        await session.commit()
        print(f"User {email} is now an admin.")
    await engine.dispose()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True, help="Email of an existing user to promote")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(promote(args.email.strip())))


if __name__ == "__main__":
    main()
