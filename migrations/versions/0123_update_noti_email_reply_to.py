"""

Revision ID: 714c1b20e578
Revises: 0122_add_service_letter_contact
Create Date: 2017-09-27 18:30:57.853295

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0123_update_noti_email_reply_to'
down_revision = '0122_add_service_letter_contact'


def upgrade():
    op.add_column('notifications', sa.Column('email_reply_to_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index(op.f('ix_notifications_email_reply_to_id'), 'notifications', ['email_reply_to_id'], unique=False)
    op.create_foreign_key(None, 'notifications', 'service_email_reply_to', ['email_reply_to_id'], ['id'])


def downgrade():
    op.drop_index(op.f('ix_notifications_email_reply_to_id'), table_name='notifications')
    op.drop_column('notifications', 'email_reply_to_id')
