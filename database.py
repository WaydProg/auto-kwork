import json
from collections.abc import Iterable

import aiosqlite

from config import DB_PATH

STATUS_NEW = 'new'
STATUS_SENT = 'sent'
STATUS_IGNORED = 'ignored'
STATUS_RESPONDED = 'responded'


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                id          INTEGER PRIMARY KEY,
                title       TEXT NOT NULL,
                description TEXT,
                price       TEXT,
                url         TEXT NOT NULL,
                files_json  TEXT,
                status      TEXT NOT NULL DEFAULT 'new'
                            CHECK (status IN ('new', 'sent', 'ignored', 'responded')),
                message_ids TEXT,
                draft       TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Миграция баз, созданных до появления AI-черновиков
        async with db.execute('PRAGMA table_info(projects)') as cur:
            columns = {row[1] async for row in cur}
        if 'draft' not in columns:
            await db.execute('ALTER TABLE projects ADD COLUMN draft TEXT')
        await db.commit()


async def get_statuses(project_ids: Iterable[int]) -> dict[int, str]:
    ids = list(project_ids)
    if not ids:
        return {}
    placeholders = ','.join('?' * len(ids))
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f'SELECT id, status FROM projects WHERE id IN ({placeholders})', ids
        ) as cur:
            return {row[0]: row[1] async for row in cur}


async def add_project(project: dict) -> None:
    """Сохраняет проект со статусом new (если его ещё нет)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            '''INSERT OR IGNORE INTO projects (id, title, description, price, url, files_json, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (
                project['id'],
                project['title'],
                project['description'],
                project['price'],
                project['url'],
                json.dumps(project['files'], ensure_ascii=False),
                STATUS_NEW,
            ),
        )
        await db.commit()


async def get_project(project_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM projects WHERE id = ?', (project_id,)) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    project = dict(row)
    project['files'] = json.loads(project['files_json'] or '[]')
    project['message_ids'] = json.loads(project['message_ids'] or '[]')
    return project


async def set_status(project_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE projects SET status = ? WHERE id = ?', (status, project_id))
        await db.commit()


async def mark_sent(project_id: int, message_ids: list[int]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'UPDATE projects SET status = ?, message_ids = ? WHERE id = ?',
            (STATUS_SENT, json.dumps(message_ids), project_id),
        )
        await db.commit()


async def set_draft(project_id: int, draft: str | None) -> None:
    """Черновик отклика (AI-генерация или правка пользователя) до подтверждения отправки."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE projects SET draft = ? WHERE id = ?', (draft, project_id))
        await db.commit()
