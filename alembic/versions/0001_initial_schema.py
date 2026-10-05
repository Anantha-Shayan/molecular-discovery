"""initial schema

Baseline for the Molecular Discovery Platform: targets, jobs, job_stage_logs,
molecules, stage_results and artifacts.

Note on `targets.artifact_id`: targets -> artifacts -> jobs -> targets is a
foreign-key cycle, so that one constraint cannot be created inline with
`targets`. It is added after `artifacts` exists (and dropped first on
downgrade). Autogenerate emits it inline, which PostgreSQL rejects, or with
use_alter, which `op.create_table` silently skips — hence the explicit step.

Revision ID: 0001
Revises:
Create Date: 2026-10-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('targets',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('source', sa.String(), nullable=False),
    sa.Column('pdb_id', sa.String(), nullable=True),
    sa.Column('artifact_id', sa.String(), nullable=True),
    sa.Column('original_filename', sa.String(), nullable=True),
    sa.Column('structure_path', sa.String(), nullable=True),
    sa.Column('structure_format', sa.String(), nullable=True),
    sa.Column('checksum', sa.String(), nullable=True),
    sa.Column('file_size_bytes', sa.Integer(), nullable=True),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('experiment_method', sa.String(), nullable=True),
    sa.Column('resolution_a', sa.Float(), nullable=True),
    sa.Column('chains', sa.JSON(), nullable=True),
    sa.Column('selected_chain', sa.String(), nullable=True),
    sa.Column('residue_count', sa.Integer(), nullable=True),
    sa.Column('atom_count', sa.Integer(), nullable=True),
    sa.Column('ligands', sa.JSON(), nullable=True),
    sa.Column('validation_status', sa.String(), nullable=True),
    sa.Column('validation_checks', sa.JSON(), nullable=True),
    sa.Column('is_demo', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('jobs',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('target_id', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('current_stage', sa.String(), nullable=True),
    sa.Column('failure_reason', sa.Text(), nullable=True),
    sa.Column('params', sa.JSON(), nullable=False),
    sa.Column('label', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['target_id'], ['targets.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_jobs_created_at'), 'jobs', ['created_at'], unique=False)
    op.create_index(op.f('ix_jobs_target_id'), 'jobs', ['target_id'], unique=False)
    op.create_table('job_stage_logs',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('job_id', sa.String(), nullable=False),
    sa.Column('stage', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('external_job_id', sa.String(), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_job_stage_logs_job_id'), 'job_stage_logs', ['job_id'], unique=False)
    op.create_table('molecules',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('job_id', sa.String(), nullable=False),
    sa.Column('display_id', sa.String(), nullable=False),
    sa.Column('smiles', sa.Text(), nullable=False),
    sa.Column('inchikey', sa.String(), nullable=True),
    sa.Column('source_stage', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_molecules_job_id'), 'molecules', ['job_id'], unique=False)
    op.create_table('artifacts',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('job_id', sa.String(), nullable=True),
    sa.Column('molecule_id', sa.String(), nullable=True),
    sa.Column('stage', sa.String(), nullable=True),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('storage_path', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.ForeignKeyConstraint(['molecule_id'], ['molecules.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_artifacts_job_id'), 'artifacts', ['job_id'], unique=False)
    op.create_foreign_key(
        'fk_targets_artifact_id', 'targets', 'artifacts', ['artifact_id'], ['id']
    )
    op.create_table('stage_results',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('molecule_id', sa.String(), nullable=False),
    sa.Column('job_id', sa.String(), nullable=False),
    sa.Column('stage', sa.String(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('is_mocked', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.ForeignKeyConstraint(['molecule_id'], ['molecules.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_stage_results_job_id'), 'stage_results', ['job_id'], unique=False)
    op.create_index(op.f('ix_stage_results_molecule_id'), 'stage_results', ['molecule_id'], unique=False)


def downgrade() -> None:
    op.drop_constraint('fk_targets_artifact_id', 'targets', type_='foreignkey')
    op.drop_index(op.f('ix_stage_results_molecule_id'), table_name='stage_results')
    op.drop_index(op.f('ix_stage_results_job_id'), table_name='stage_results')
    op.drop_table('stage_results')
    op.drop_index(op.f('ix_artifacts_job_id'), table_name='artifacts')
    op.drop_table('artifacts')
    op.drop_index(op.f('ix_molecules_job_id'), table_name='molecules')
    op.drop_table('molecules')
    op.drop_index(op.f('ix_job_stage_logs_job_id'), table_name='job_stage_logs')
    op.drop_table('job_stage_logs')
    op.drop_index(op.f('ix_jobs_target_id'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_created_at'), table_name='jobs')
    op.drop_table('jobs')
    op.drop_table('targets')
