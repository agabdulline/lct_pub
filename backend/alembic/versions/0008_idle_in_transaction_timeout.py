"""Сессия, забытая посреди транзакции, закрывается сервером через 5 минут.

Инцидент на проде 26.09: соединение api почти 2 часа висело «idle in transaction» и держало блокировку `runs`, а
миграция с `ALTER TABLE runs` ждала её 21 минуту — за ней в очередь встали все запросы к `runs`. Параметр — на базу:
для всех ролей (владелец, `lct_ml`) и новых сессий. Штатным операциям 5 минут простоя внутри транзакции не нужно:
загрузчик и сверка базы работают в autocommit или внутри COPY, задачи и эндпоинты держат транзакцию секунды —
самое долгое ожидание внутри неё — запрос к ml-service, его предел 120 с.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

TIMEOUT = "5min"


def _database() -> str:
    name = op.get_bind().execute(sa.text("SELECT current_database()")).scalar_one()
    return '"' + name.replace('"', '""') + '"'


def upgrade() -> None:
    op.execute(f"ALTER DATABASE {_database()} SET idle_in_transaction_session_timeout = '{TIMEOUT}'")


def downgrade() -> None:
    op.execute(f"ALTER DATABASE {_database()} RESET idle_in_transaction_session_timeout")
