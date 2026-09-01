import asyncio
import sys
from pathlib import Path

import asyncpg

from app.config import settings


async def apply_migration(file_path: Path) -> None:
    sql = file_path.read_text(encoding="utf-8")

    connection = await asyncpg.connect(
        settings.database_url.get_secret_value()
    )

    try:
        async with connection.transaction():
            await connection.execute(sql)
    finally:
        await connection.close()


async def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(
            "Usage: python -m scripts.apply_migration <migration-file>"
        )

    file_path = Path(sys.argv[1])

    if not file_path.is_file():
        raise SystemExit(f"Migration file not found: {file_path}")

    print(f"Applying migration: {file_path}")

    await apply_migration(file_path)

    print("Migration applied successfully")


if __name__ == "__main__":
    asyncio.run(main())
