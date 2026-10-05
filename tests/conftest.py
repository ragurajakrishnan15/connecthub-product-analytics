"""Shared fixtures."""
import matplotlib
import pytest

import generate_synthetic_data as gen

matplotlib.use('Agg')  # cohort_engine plots; never open a window in tests

SAMPLE_USERS = 4000
SAMPLE_WORKSPACES = 400


@pytest.fixture(scope='session')
def sample_data(tmp_path_factory):
    """A small generated dataset plus experiment assignments, built once per run."""
    from experimentation.assignment import assign_from_parquet

    out = tmp_path_factory.mktemp('data')
    gen.generate_dataset(str(out), users=SAMPLE_USERS, workspaces=SAMPLE_WORKSPACES,
                         seed=42, verbose=False)
    assign_from_parquet(str(out / 'users.parquet'), gen.EXPERIMENT_ID) \
        .to_parquet(out / 'experiment_assignments.parquet', index=False)
    return out
