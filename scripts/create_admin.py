"""Explicit local bootstrap; no public admin-registration endpoint."""

import argparse
import getpass

from app.controllers.auth import create_party, find_email
from app.db.session import SessionLocal

parser = argparse.ArgumentParser()
parser.add_argument("email")
args = parser.parse_args()
password = getpass.getpass("Admin password (12+ characters): ")
if len(password) < 12:
    raise SystemExit("Use at least 12 characters")
with SessionLocal.begin() as db:
    if find_email(db, args.email):
        raise SystemExit("Account already exists")
    party = create_party(db, args.email, password, "ADMIN", "Administrator", "TAMP")
    party.data = {**party.data, "verification": "VERIFIED"}
print("Administrator created")
