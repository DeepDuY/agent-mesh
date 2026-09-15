from __future__ import annotations

from agent_mesh.orchestrator.store.base import AbstractStore
from agent_mesh.orchestrator.store.connection import PostgresDatabase


class PostgresBase(AbstractStore):
    """Base class for PostgreSQL store mixins.

    Owns a :class:`PostgresDatabase` connection layer (per-process lazy pool +
    schema bootstrap); domain mixins only write SQL and row mapping.
    """

    def __init__(
        self,
        *,
        dsn: str = "",
        host: str = "127.0.0.1",
        port: int = 5432,
        user: str = "agent_mesh",
        password: str = "",
        database: str = "agent_mesh",
        min_size: int = 2,
        max_size: int = 10,
    ):
        self._db = PostgresDatabase(
            dsn=dsn,
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            min_size=min_size,
            max_size=max_size,
        )

    async def initialize(self) -> None:
        await self._db.initialize()

    async def close(self) -> None:
        await self._db.close()
