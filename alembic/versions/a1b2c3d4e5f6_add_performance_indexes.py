"""add performance indexes

Revision ID: a1b2c3d4e5f6
Revises: b2c3d4e5f6g7
Create Date: 2026-05-19 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6g7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _index_exists(bind, table_name, index_name):
    inspector = inspect(bind)
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade() -> None:
    bind = op.get_bind()

    # pickup_codes 表索引
    if not _index_exists(bind, 'pickup_codes', 'ix_pickup_codes_file_id'):
        op.create_index('ix_pickup_codes_file_id', 'pickup_codes', ['file_id'])
        print("[成功] 已创建索引 ix_pickup_codes_file_id")
    else:
        print("[提示] ix_pickup_codes_file_id 已存在，跳过")

    if not _index_exists(bind, 'pickup_codes', 'ix_pickup_codes_status'):
        op.create_index('ix_pickup_codes_status', 'pickup_codes', ['status'])
        print("[成功] 已创建索引 ix_pickup_codes_status")
    else:
        print("[提示] ix_pickup_codes_status 已存在，跳过")

    if not _index_exists(bind, 'pickup_codes', 'ix_pickup_codes_expire_at'):
        op.create_index('ix_pickup_codes_expire_at', 'pickup_codes', ['expire_at'])
        print("[成功] 已创建索引 ix_pickup_codes_expire_at")
    else:
        print("[提示] ix_pickup_codes_expire_at 已存在，跳过")

    # files 表索引
    if not _index_exists(bind, 'files', 'ix_files_uploader_id'):
        op.create_index('ix_files_uploader_id', 'files', ['uploader_id'])
        print("[成功] 已创建索引 ix_files_uploader_id")
    else:
        print("[提示] ix_files_uploader_id 已存在，跳过")


def downgrade() -> None:
    bind = op.get_bind()

    if _index_exists(bind, 'pickup_codes', 'ix_pickup_codes_file_id'):
        op.drop_index('ix_pickup_codes_file_id', table_name='pickup_codes')

    if _index_exists(bind, 'pickup_codes', 'ix_pickup_codes_status'):
        op.drop_index('ix_pickup_codes_status', table_name='pickup_codes')

    if _index_exists(bind, 'pickup_codes', 'ix_pickup_codes_expire_at'):
        op.drop_index('ix_pickup_codes_expire_at', table_name='pickup_codes')

    if _index_exists(bind, 'files', 'ix_files_uploader_id'):
        op.drop_index('ix_files_uploader_id', table_name='files')
