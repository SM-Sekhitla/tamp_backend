"""Explicit local bootstrap; no public admin-registration endpoint."""

import argparse
import asyncio
import getpass

from app.controllers.auth import create_party, find_email
from app.db.session import AsyncSessionLocal


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("email")
    args = parser.parse_args()
    password = getpass.getpass("Admin password (12+ characters): ")
    if len(password) < 12:
        raise SystemExit("Use at least 12 characters")

    async with AsyncSessionLocal.begin() as db:

        def create(sync_db):
            if find_email(sync_db, args.email):
                raise SystemExit("Account already exists")
            party = create_party(
                sync_db,
                args.email,
                password,
                "ADMIN",
                "Administrator",
                "TAMP",
            )
            party.data = {**party.data, "verification": "VERIFIED"}

        await db.run_sync(create)
    print("Administrator created")


if __name__ == "__main__":
    asyncio.run(main())
