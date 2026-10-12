import asyncio
import os

import pytest
from fastapi.testclient import TestClient

url = os.environ.get("TEST_DATABASE_URL", "")
if not url or "test" not in url.rsplit("/", 1)[-1]:
    raise RuntimeError(
        "Set TEST_DATABASE_URL to a dedicated PostgreSQL database with test in its name"
    )
if url.startswith("postgresql+psycopg://"):
    url = url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
os.environ.update(
    DATABASE_URL=url,
    DEV_EMAIL_CODES="true",
    AUTH_RATE_LIMIT="1000",
    RATE_LIMIT="10000",
)

from app.controllers.auth import create_party
from app.core.config import settings
from app.db.base import Base
from app.db.session import AsyncSessionLocal, engine
from app.main import app


def async_run(coroutine):
    return asyncio.run(coroutine)


def db_call(callback):
    async def invoke():
        async with AsyncSessionLocal.begin() as db:
            return await db.run_sync(callback)

    return async_run(invoke())


@pytest.fixture(autouse=True)
def clean_database(tmp_path):
    from sqlalchemy import text

    async def clean():
        async with engine.begin() as connection:
            names = ", ".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
            await connection.execute(text("TRUNCATE " + names + " RESTART IDENTITY CASCADE"))

    async_run(clean())
    settings.upload_path = str(tmp_path / "uploads")


@pytest.fixture
def client():
    with TestClient(app, headers={"X-TAMP-Request": "1", "Host": "localhost"}) as test_client:
        yield test_client


def rpc(client, name, data=None, mutation=False):
    envelope = {"0": {"json": data or {}}}
    if mutation:
        return client.post("/api/trpc/" + name + "?batch=1", json=envelope)
    import json

    return client.get(
        "/api/trpc/" + name,
        params={"batch": "1", "input": json.dumps(envelope)},
    )


def result(response):
    assert response.status_code == 200, response.text
    return response.json()[0]["result"]["data"]["json"]


@pytest.fixture
def accounts():
    users = {}

    def create_accounts(db):
        for key, role in [
            ("owner", "FREIGHT_OWNER"),
            ("other", "FREIGHT_OWNER"),
            ("carrier", "TRANSPORTER"),
            ("driver", "DRIVER"),
            ("admin", "ADMIN"),
        ]:
            party = create_party(
                db,
                key + "@example.com",
                "Testing-password-123",
                role,
                key,
                key + " Co",
            )
            party.data = {**party.data, "verification": "VERIFIED"}
            users[key] = dict(party.data)

    db_call(create_accounts)
    return users


def login(client, key):
    return result(
        rpc(
            client,
            "auth.login",
            {
                "email": key + "@example.com",
                "password": "Testing-password-123",
            },
            True,
        )
    )
