"""Applies the 5 database migration SQL files in order and seeds initial data."""
import asyncio
import os
from pathlib import Path
import asyncpg
import seed_100k

DSN = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/chronosdb")

SQL_FILES = [
    "01_schema_bcnf.sql",
    "02_concurrency_procs.sql",
    "03_self_healing_triggers.sql",
    "04_time_travel_views.sql",
    "05_indexes_benchmark.sql",
]


async def run_migrations():
    print(f"Connecting to database: {DSN.split('@')[-1]}")
    conn = await asyncpg.connect(DSN)
    
    # Check if already migrated and seeded
    try:
        table_exists = await conn.fetchval("SELECT to_regclass('public.users')")
        if table_exists:
            count = await conn.fetchval("SELECT count(*) FROM users")
            if count and count >= 2000:
                print(f"Database already initialized with {count} users. Skipping setup.")
                await conn.close()
                return True
    except Exception as e:
        print(f"Check existing tables note: {e}")
    
    # Locate database folder
    db_dir = Path(__file__).resolve().parent.parent / "database"
    if not db_dir.exists():
        db_dir = Path(__file__).resolve().parent / "database"

    print(f"Applying SQL migrations from: {db_dir}")
    for fname in SQL_FILES:
        fpath = db_dir / fname
        if not fpath.exists():
            print(f"Error: {fname} not found at {fpath}")
            await conn.close()
            return False
        print(f"  Executing {fname} ...")
        sql = fpath.read_text(encoding="utf-8")
        await conn.execute(sql)
    
    print("All 5 SQL files executed successfully!")
    await conn.close()
    
    print("Starting data seeding (seed_100k) ...")
    await seed_100k.main(force=True)
    print("Database setup and seeding complete!")
    return True


if __name__ == "__main__":
    asyncio.run(run_migrations())
