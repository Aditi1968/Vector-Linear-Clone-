"""Temporary connectivity check for the Phase 0 asyncpg pool.

Verifies that app.db can open a pool against the configured database, run a
trivial query, and close cleanly. Creates nothing and reads nothing sensitive.
"""

import asyncio

from app import db


async def main() -> None:
    await db.connect()
    print("Pool created successfully")

    pool = db.get_pool()
    value = await pool.fetchval("SELECT 1")
    print(f"Postgres returned: {value}")

    await db.disconnect()
    print("Disconnected successfully")


asyncio.run(main())
