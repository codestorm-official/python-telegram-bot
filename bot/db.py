"""PostgreSQL access layer for ELITA Finance."""

import logging
import asyncpg

logger = logging.getLogger(__name__)

POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 10
COMMAND_TIMEOUT = 10.0

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_TRANSACTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS transactions (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    account_type TEXT NOT NULL
        CHECK (account_type IN ('company', 'personal')),
    transaction_type TEXT NOT NULL
        CHECK (transaction_type IN ('expense', 'income')),
    amount BIGINT NOT NULL CHECK (amount > 0),
    category TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_transactions_user_date
ON transactions (telegram_id, created_at DESC);
"""

UPSERT_USER = """
INSERT INTO users (telegram_id, username, first_name)
VALUES ($1, $2, $3)
ON CONFLICT (telegram_id) DO UPDATE
SET username = EXCLUDED.username,
    first_name = EXCLUDED.first_name,
    last_seen = now()
RETURNING (xmax = 0) AS is_new;
"""


async def create_pool(dsn: str) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        dsn,
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        command_timeout=COMMAND_TIMEOUT,
    )
    async with pool.acquire() as conn:
        await conn.execute(CREATE_USERS_TABLE)
        await conn.execute(CREATE_TRANSACTIONS_TABLE)
    logger.info("PostgreSQL tables ready.")
    return pool


async def upsert_user(
    pool: asyncpg.Pool,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
) -> bool:
    result = await pool.fetchval(
        UPSERT_USER, telegram_id, username, first_name
    )
    return bool(result)


async def add_transaction(
    pool: asyncpg.Pool,
    telegram_id: int,
    account_type: str,
    transaction_type: str,
    amount: int,
    category: str,
    description: str = "",
) -> int:
    return await pool.fetchval(
        """
        INSERT INTO transactions
        (telegram_id, account_type, transaction_type,
         amount, category, description)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id;
        """,
        telegram_id, account_type, transaction_type,
        amount, category, description,
    )


async def get_balance(
    pool: asyncpg.Pool,
    telegram_id: int,
    account_type: str,
) -> int:
    result = await pool.fetchval(
        """
        SELECT COALESCE(SUM(
            CASE WHEN transaction_type = 'income'
                 THEN amount ELSE -amount END
        ), 0)
        FROM transactions
        WHERE telegram_id = $1 AND account_type = $2;
        """,
        telegram_id, account_type,
    )
    return int(result)


async def get_report(pool: asyncpg.Pool, telegram_id: int) -> list:
    return await pool.fetch(
        """
        SELECT account_type, transaction_type, category,
               SUM(amount) AS total, COUNT(*) AS operations
        FROM transactions
        WHERE telegram_id = $1
          AND created_at >= date_trunc('month', now())
        GROUP BY account_type, transaction_type, category
        ORDER BY account_type, transaction_type, category;
        """,
        telegram_id,
    )


async def count_users(pool: asyncpg.Pool) -> int:
    return int(await pool.fetchval("SELECT count(*) FROM users;"))


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
    logger.info("PostgreSQL pool closed.")
