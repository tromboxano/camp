"""Small SQLAlchemy adapter preserving the SQLite-style interface used by AppCampo."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine


class Row(dict[str, Any]):
    """Mapping row that also supports SQLite-style integer indexing."""

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class Result:
    def __init__(self, rows: list[Row] | None = None, rowcount: int = -1,
                 lastrowid: int | None = None) -> None:
        self._rows = rows or []
        self._index = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self) -> Row | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self) -> list[Row]:
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows

    def __iter__(self) -> Iterator[Row]:
        return iter(self.fetchall())


def _database_url() -> str:
    url = os.environ.get('DATABASE_URL', 'sqlite:///campo.db').strip()
    if url.startswith('postgres://'):
        url = 'postgresql://' + url.removeprefix('postgres://')
    if url.startswith('postgresql://'):
        url = 'postgresql+psycopg://' + url.removeprefix('postgresql://')
    return url


class Database:
    def __init__(self, url: str | None = None) -> None:
        self.url = _database_url() if url is None else url
        options: dict[str, Any] = {'pool_pre_ping': True}
        if self.url.startswith('sqlite:'):
            options['connect_args'] = {'check_same_thread': False}
        self.engine: Engine = create_engine(self.url, **options)
        self._local = threading.local()
        self.audit_enabled = False
        self.actor_provider = None
        self.owner_provider = None

    @property
    def is_postgresql(self) -> bool:
        return self.engine.dialect.name == 'postgresql'

    def _connection(self) -> Connection:
        connection = getattr(self._local, 'connection', None)
        if connection is None or connection.closed:
            connection = self.engine.connect()
            self._local.connection = connection
            self._local.dirty = False
        return connection

    @staticmethod
    def _bind_parameters(sql: str, parameters: Sequence[Any] | Mapping[str, Any] | None
                         ) -> tuple[str, dict[str, Any]]:
        if parameters is None or isinstance(parameters, Mapping):
            return sql, dict(parameters or {})
        index = 0

        def replace_placeholder(_: re.Match[str]) -> str:
            nonlocal index
            name = f'p{index}'
            index += 1
            return f':{name}'

        bound_sql = re.sub(r'\?', replace_placeholder, sql)
        bound_parameters = {f'p{i}': value for i, value in enumerate(parameters)}
        if index != len(bound_parameters):
            raise ValueError('SQL placeholder count does not match parameter count')
        return bound_sql, bound_parameters

    def execute(self, sql: str, parameters: Sequence[Any] | Mapping[str, Any] | None = None
                ) -> Result:
        statement = sql.strip().rstrip(';')
        keyword = statement.split(None, 1)[0].upper() if statement else ''
        is_insert = keyword == 'INSERT'
        if is_insert and ' RETURNING ' not in statement.upper():
            statement += ' RETURNING id'
        bound_sql, bound_parameters = self._bind_parameters(statement, parameters)
        connection = self._connection()
        try:
            result = connection.execute(text(bound_sql), bound_parameters)
            if keyword in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'ALTER', 'CREATE', 'DROP'}:
                self._local.dirty = True

            rows = [Row(row) for row in result.mappings().all()] if result.returns_rows else []
            lastrowid = rows[0].get('id') if is_insert and rows else None
            self._write_audit(
                connection,
                statement,
                keyword,
                bound_parameters,
                lastrowid,
            )
            wrapped = Result(rows, result.rowcount, lastrowid)
        except Exception:
            self.rollback()
            raise
        if not getattr(self._local, 'dirty', False):
            self._close_connection()
        return wrapped

    def _write_audit(self, connection: Connection, statement: str, keyword: str,
                     parameters: Mapping[str, Any], inserted_id: int | None) -> None:
        if not self.audit_enabled or keyword not in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
            return
        match = re.match(
            r'\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+["`\[]?([a-zA-Z_]\w*)',
            statement,
            flags=re.IGNORECASE,
        )
        if not match or match.group(1).lower() == 'auditoria':
            return

        table_name = match.group(1)
        action = {'INSERT': 'crear', 'REPLACE': 'crear', 'UPDATE': 'modificar',
                  'DELETE': 'eliminar'}[keyword]
        entity_id = inserted_id
        if entity_id is None:
            where_match = re.search(r'\bWHERE\s+id\s*=\s*\?', statement, flags=re.IGNORECASE)
            if where_match:
                placeholder_index = statement[:where_match.end()].count('?') - 1
                entity_id = parameters.get(f'p{placeholder_index}')
        actor_id, actor_name = self.actor_provider() if self.actor_provider else (None, None)
        owner_id = self.owner_provider() if self.owner_provider else actor_id
        safe_parameters = (
            {'redacted': True}
            if table_name.lower() == 'usuarios'
            else parameters
        )
        details = json.dumps(
            {'statement': statement, 'parameters': safe_parameters},
            ensure_ascii=False,
            default=str,
        )
        connection.execute(
            text(
                'INSERT INTO auditoria(actor_id, actor_name, action, entity_type, '
                'entity_id, owner_id, details, created_at) '
                'VALUES(:actor_id, :actor_name, :action, :entity_type, :entity_id, '
                ':owner_id, :details, :created_at)'
            ),
            {
                'actor_id': actor_id,
                'actor_name': actor_name,
                'action': action,
                'entity_type': table_name,
                'entity_id': entity_id,
                'owner_id': owner_id,
                'details': details,
                'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            },
        )

    def executescript(self, script: str) -> None:
        if self.is_postgresql:
            script = re.sub(
                r'\bINTEGER\s+PRIMARY\s+KEY\b',
                'INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY',
                script,
                flags=re.IGNORECASE,
            )
        for statement in script.split(';'):
            if statement.strip():
                self.execute(statement)
        self.commit()

    def table_columns(self, table_name: str) -> set[str]:
        return {column['name'] for column in inspect(self.engine).get_columns(table_name)}

    def commit(self) -> None:
        connection = getattr(self._local, 'connection', None)
        if connection is not None and not connection.closed:
            connection.commit()
        self._close_connection()

    def rollback(self) -> None:
        connection = getattr(self._local, 'connection', None)
        if connection is not None and not connection.closed:
            connection.rollback()
        self._close_connection()

    def _close_connection(self) -> None:
        connection = getattr(self._local, 'connection', None)
        if connection is not None and not connection.closed:
            connection.close()
        self._local.connection = None
        self._local.dirty = False

    def close(self) -> None:
        self._close_connection()
        self.engine.dispose()
