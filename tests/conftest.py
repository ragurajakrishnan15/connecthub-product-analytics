"""Shared fixtures."""
import matplotlib
import pytest

import generate_synthetic_data as gen

matplotlib.use('Agg')  # cohort_engine plots; never open a window in tests

GOOGLE_SUFFIXES = ('googleapis.com', 'google.com', 'gstatic.com', 'ai.google.dev')


@pytest.fixture(autouse=True)
def never_resolve_a_google_host(monkeypatch):
    """No test may reach Google, whatever it builds (Phase 6: the live Gemini evaluation is a separate,
    approved step). Resolving such a name fails the test; every other name resolves as before."""
    import socket
    real = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else str(host or '')
        if name.lower().rstrip('.').endswith(GOOGLE_SUFFIXES):
            raise AssertionError(f'a test tried to reach {name}')
        return real(host, *args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', guarded)


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
