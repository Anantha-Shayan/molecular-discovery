"""campaign execution model: job stages, batches, molecule outcomes

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('cancel_requested', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('jobs', sa.Column('started_at', sa.DateTime(), nullable=True))
    op.add_column('jobs', sa.Column('finished_at', sa.DateTime(), nullable=True))

    op.add_column('molecules', sa.Column('source', sa.String(), nullable=True))
    op.add_column('molecules', sa.Column('external_id', sa.String(), nullable=True))
    op.add_column('molecules', sa.Column('outcome', sa.String(), nullable=False, server_default='active'))
    op.add_column('molecules', sa.Column('exit_stage', sa.String(), nullable=True))
    op.add_column('molecules', sa.Column('exit_reason', sa.Text(), nullable=True))

    op.add_column('stage_results', sa.Column('status', sa.String(), nullable=False, server_default='succeeded'))
    op.add_column('stage_results', sa.Column('error', sa.Text(), nullable=True))
    op.add_column('stage_results', sa.Column('batch_id', sa.String(), nullable=True))
    op.create_index('ix_stage_results_batch_id', 'stage_results', ['batch_id'])

    op.create_table('job_stages',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('job_id', sa.String(), nullable=False),
        sa.Column('stage', sa.String(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('count_in', sa.Integer(), nullable=False),
        sa.Column('count_out', sa.Integer(), nullable=False),
        sa.Column('count_failed', sa.Integer(), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('tool', sa.String(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_job_stages_job_id', 'job_stages', ['job_id'])

    op.create_table('batches',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('job_id', sa.String(), nullable=False),
        sa.Column('stage', sa.String(), nullable=False),
        sa.Column('index', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('molecule_ids', sa.JSON(), nullable=False),
        sa.Column('ok_count', sa.Integer(), nullable=False),
        sa.Column('failed_count', sa.Integer(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('log_path', sa.String(), nullable=True),
        sa.Column('seconds', sa.Float(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_batches_job_id', 'batches', ['job_id'])
    op.create_index('ix_batches_stage', 'batches', ['stage'])


def downgrade() -> None:
    op.drop_table('batches')
    op.drop_table('job_stages')
    op.drop_index('ix_stage_results_batch_id', table_name='stage_results')
    for col in ('batch_id', 'error', 'status'):
        op.drop_column('stage_results', col)
    for col in ('exit_reason', 'exit_stage', 'outcome', 'external_id', 'source'):
        op.drop_column('molecules', col)
    for col in ('finished_at', 'started_at', 'cancel_requested'):
        op.drop_column('jobs', col)
