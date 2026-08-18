"""PostgreSQL access layer backed by an asyncpg connection pool."""

import logging
from collections.abc import Sequence

import asyncpg


logger = logging.getLogger(__name__)

# Connection/command guards so a stalled database never hangs the bot.
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 10
COMMAND_TIMEOUT = 10.0

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_ANONYMOUS_SUBMISSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS anonymous_submissions (
    submission_id TEXT PRIMARY KEY,
    source_chat_id BIGINT NOT NULL,
    source_message_ids BIGINT[] NOT NULL,
    media_group_id TEXT,
    review_chat_id BIGINT NOT NULL,
    review_message_id BIGINT,
    status TEXT NOT NULL DEFAULT 'pending',
    moderator_id BIGINT,
    published_message_ids BIGINT[],
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at TIMESTAMPTZ
);
"""

UPSERT_USER = """
INSERT INTO users (telegram_id, username, first_name)
VALUES ($1, $2, $3)
ON CONFLICT (telegram_id) DO UPDATE
    SET username   = EXCLUDED.username,
        first_name = EXCLUDED.first_name,
        last_seen  = now()
RETURNING (xmax = 0) AS is_new;
"""


async def create_pool(dsn: str) -> asyncpg.Pool:
    """Open the connection pool and ensure the schema exists. Raises on failure."""
    pool = await asyncpg.create_pool(
        dsn,
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        command_timeout=COMMAND_TIMEOUT,
    )
    async with pool.acquire() as conn:
        await conn.execute(CREATE_USERS_TABLE)
        await conn.execute(CREATE_ANONYMOUS_SUBMISSIONS_TABLE)
    logger.info("PostgreSQL pool ready (schema initialized).")
    return pool


async def upsert_user(
    pool: asyncpg.Pool,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
) -> bool:
    """Insert or refresh a user. Returns True if this is a brand-new user."""
    is_new = await pool.fetchval(UPSERT_USER, telegram_id, username, first_name)
    return bool(is_new)


async def count_users(pool: asyncpg.Pool) -> int:
    """Return the total number of known users."""
    return int(await pool.fetchval("SELECT count(*) FROM users;"))


async def create_anonymous_submission(
    pool: asyncpg.Pool,
    submission_id: str,
    source_chat_id: int,
    source_message_ids: Sequence[int],
    media_group_id: str | None,
    review_chat_id: int,
) -> None:
    await pool.execute(
        """
        INSERT INTO anonymous_submissions (
            submission_id,
            source_chat_id,
            source_message_ids,
            media_group_id,
            review_chat_id
        )
        VALUES ($1, $2, $3, $4, $5);
        """,
        submission_id,
        source_chat_id,
        list(source_message_ids),
        media_group_id,
        review_chat_id,
    )


async def attach_review_message(pool: asyncpg.Pool, submission_id: str, review_message_id: int) -> None:
    await pool.execute(
        """
        UPDATE anonymous_submissions
        SET review_message_id = $2
        WHERE submission_id = $1;
        """,
        submission_id,
        review_message_id,
    )


async def get_anonymous_submission(pool: asyncpg.Pool, submission_id: str) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """
        SELECT *
        FROM anonymous_submissions
        WHERE submission_id = $1;
        """,
        submission_id,
    )


async def mark_anonymous_submission_approved(
    pool: asyncpg.Pool,
    submission_id: str,
    moderator_id: int,
    published_message_ids: Sequence[int],
) -> None:
    await pool.execute(
        """
        UPDATE anonymous_submissions
        SET status = 'approved',
            moderator_id = $2,
            published_message_ids = $3,
            decided_at = now(),
            error_message = NULL
        WHERE submission_id = $1;
        """,
        submission_id,
        moderator_id,
        list(published_message_ids),
    )


async def mark_anonymous_submission_rejected(
    pool: asyncpg.Pool, submission_id: str, moderator_id: int
) -> None:
    await pool.execute(
        """
        UPDATE anonymous_submissions
        SET status = 'rejected',
            moderator_id = $2,
            decided_at = now(),
            error_message = NULL
        WHERE submission_id = $1;
        """,
        submission_id,
        moderator_id,
    )


async def mark_anonymous_submission_failed(pool: asyncpg.Pool, submission_id: str, error_message: str) -> None:
    await pool.execute(
        """
        UPDATE anonymous_submissions
        SET status = 'failed',
            error_message = $2,
            decided_at = now()
        WHERE submission_id = $1;
        """,
        submission_id,
        error_message,
    )


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
    logger.info("PostgreSQL pool closed.")
