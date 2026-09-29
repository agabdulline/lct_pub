"""Роль только на чтение для ml-service (D2): признаки читают историю из рабочей базы, писать сервису нечего.

Роль `lct_ml` — на весь сервер PostgreSQL: тестовая база `<имя>_test` на том же сервере переиспользует её.
Пароль — `ML_DB_PASSWORD` из окружения (на проде генерируется в infra/.env, DEPLOY.md), без него — пароль
стендов разработки — `lct_ml_dev`. У уже существующей роли пароль меняется, только если `ML_DB_PASSWORD` задан.
Права: подключение к базе, чтение таблиц схемы public — и тех, что создадут следующие миграции (права по умолчанию
владельца). Чанки гипертаблиц читаются через гипертаблицу, отдельных прав не требуют. Сверх прав транзакции роли —
только на чтение: запись упадёт, даже если право на таблицу когда-нибудь выдадут по ошибке.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-26
"""

import os
import re

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

ROLE = "lct_ml"
DEV_PASSWORD = "lct_ml_dev"  # как по умолчанию в infra/docker-compose.yml


def _literal(value: str) -> str:
    # пароль идёт в DDL текстом (параметры в CREATE ROLE не подставляются); на проде он из openssl rand -hex
    if not re.fullmatch(r"[A-Za-z0-9_.+-]{8,128}", value):
        raise ValueError("ML_DB_PASSWORD: 8–128 символов из латиницы, цифр и _.+-")
    return f"'{value}'"


def _database() -> str:
    name = op.get_bind().execute(sa.text("SELECT current_database()")).scalar_one()
    return '"' + name.replace('"', '""') + '"'


# Роль и её настройки — в общих каталогах сервера (`pg_authid`, `pg_db_role_setting`): миграции тестовых баз разных
# сессий pytest пишут одну и ту же строку, и одновременные записи падают «tuple concurrently updated». Поэтому роль
# создаётся с перехватом гонки, а настройка пишется, только если её ещё нет.
_READ_ONLY_SET = sa.text(
    "SELECT 1 FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole "
    "WHERE r.rolname = :r AND s.setdatabase = 0 AND 'default_transaction_read_only=on' = ANY (s.setconfig)"
)


def upgrade() -> None:
    password = os.environ.get("ML_DB_PASSWORD")
    bind = op.get_bind()
    exists = bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": ROLE}).scalar()
    if not exists:
        op.execute(
            f"DO $$ BEGIN CREATE ROLE {ROLE} LOGIN PASSWORD {_literal(password or DEV_PASSWORD)}; "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
    elif password:
        op.execute(f"ALTER ROLE {ROLE} WITH LOGIN PASSWORD {_literal(password)}")
    if not bind.execute(_READ_ONLY_SET, {"r": ROLE}).scalar():
        op.execute(f"ALTER ROLE {ROLE} SET default_transaction_read_only = on")
    op.execute(f"GRANT CONNECT ON DATABASE {_database()} TO {ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {ROLE}")


def downgrade() -> None:
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM {ROLE}")
    op.execute(f"REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE CONNECT ON DATABASE {_database()} FROM {ROLE}")
    # роль общая для баз сервера: пока у неё есть права в другой базе (тестовой), она остаётся
    op.execute(
        f"DO $$ BEGIN DROP ROLE {ROLE}; EXCEPTION WHEN dependent_objects_still_exist THEN "
        f"RAISE NOTICE 'роль {ROLE} оставлена: есть права в других базах'; END $$"
    )
