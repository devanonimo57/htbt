"""SQLite-only durable connection policy. No transport or handler retries."""
import os
import sqlite3
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

SQLITE_BUSY_TIMEOUT_MS = 5000


def sqlite_wal_runtime_is_patched(version):
    """WAL-reset fix releases, including the officially maintained backports."""
    version = tuple(version)
    return (version >= (3, 51, 3)
            or (3, 50, 7) <= version < (3, 51, 0)
            or (3, 44, 6) <= version < (3, 45, 0))


def create_application_engine(url, **kwargs):
    parsed = make_url(url)
    sqlite_file = (parsed.get_backend_name() == 'sqlite'
                   and parsed.database not in (None, '', ':memory:')
                   and parsed.query.get('mode') != 'memory')
    allow_legacy = os.getenv("HOTBOT_ALLOW_LEGACY_SQLITE", "0").strip().lower() in ("1", "true", "yes")
    if sqlite_file and not allow_legacy and not sqlite_wal_runtime_is_patched(sqlite3.sqlite_version_info):
        raise RuntimeError(
            f'SQLite {sqlite3.sqlite_version} is unsafe for concurrent WAL; '
            'install an isolated runtime with SQLite 3.51.3+ '
            '(or fixed backport 3.44.6/3.50.7) before starting HotBot. '
            'Define HOTBOT_ALLOW_LEGACY_SQLITE=1 to bypass at your own risk.')
    engine = create_async_engine(url, **kwargs)
    if sqlite_file:
        @event.listens_for(engine.sync_engine, 'connect')
        def configure_sqlite(connection, _record):
            cursor = connection.cursor()
            try:
                cursor.execute(f'PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}')
                cursor.execute('PRAGMA journal_mode=WAL')
                mode = cursor.fetchone()[0]
                if str(mode).lower() != 'wal':
                    raise RuntimeError(f'SQLite refused WAL mode: {mode}')
                cursor.execute('PRAGMA synchronous=FULL')
            finally:
                cursor.close()
    return engine
