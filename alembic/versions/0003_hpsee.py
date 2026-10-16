"""BioSolveIT HPSee integration: remote workflows and their result entries

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('hpsee_runs',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('job_id', sa.String(), nullable=False),
        sa.Column('batch_id', sa.String(), nullable=True),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('round', sa.Integer(), nullable=False),
        sa.Column('csd_id', sa.String(), nullable=True),
        sa.Column('workflow_id', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('hpsee_status', sa.Integer(), nullable=True),
        sa.Column('hpsee_status_text', sa.String(), nullable=True),
        sa.Column('tasks', sa.JSON(), nullable=True),
        sa.Column('result_library_id', sa.String(), nullable=True),
        sa.Column('result_size', sa.Integer(), nullable=True),
        sa.Column('fetched_count', sa.Integer(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('error_category', sa.String(), nullable=True),
        sa.Column('retryable', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('selected_entry_ids', sa.JSON(), nullable=True),
        sa.Column('decision', sa.String(), nullable=True),
        sa.Column('summary', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('submitted_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_hpsee_runs_job_id', 'hpsee_runs', ['job_id'])
    op.create_index('ix_hpsee_runs_batch_id', 'hpsee_runs', ['batch_id'])
    op.create_index('ix_hpsee_runs_workflow_id', 'hpsee_runs', ['workflow_id'])

    op.create_table('hpsee_entries',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('job_id', sa.String(), nullable=False),
        sa.Column('entry_id', sa.BigInteger(), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=True),
        sa.Column('smiles', sa.Text(), nullable=True),
        sa.Column('n_linkers', sa.Integer(), nullable=False),
        sa.Column('docking_score', sa.Float(), nullable=True),
        sa.Column('hyde_kj_mol', sa.Float(), nullable=True),
        sa.Column('hyde', sa.JSON(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('pose_path', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['run_id'], ['hpsee_runs.id']),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_hpsee_entries_run_id', 'hpsee_entries', ['run_id'])
    op.create_index('ix_hpsee_entries_job_id', 'hpsee_entries', ['job_id'])


def downgrade() -> None:
    op.drop_index('ix_hpsee_entries_job_id', table_name='hpsee_entries')
    op.drop_index('ix_hpsee_entries_run_id', table_name='hpsee_entries')
    op.drop_table('hpsee_entries')
    op.drop_index('ix_hpsee_runs_workflow_id', table_name='hpsee_runs')
    op.drop_index('ix_hpsee_runs_batch_id', table_name='hpsee_runs')
    op.drop_index('ix_hpsee_runs_job_id', table_name='hpsee_runs')
    op.drop_table('hpsee_runs')
