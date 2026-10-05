"""
Pipeline steps. Each takes a Settings object and returns a metrics dict; a
failed step raises PipelineError with a one-line explanation.

    generate -> assign -> ingest -> validate_bronze -> dbt -> validate_gold
             -> analytics -> validate_analytics

All steps are idempotent: rerunning any of them with the same settings leaves
the warehouse in the same state.
"""
import hashlib
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field

from pipeline import config

STEP_ORDER = ['generate', 'assign', 'ingest', 'validate_bronze', 'dbt', 'validate_gold',
              'analytics', 'validate_analytics']
MANIFEST = '_manifest.json'


class PipelineError(RuntimeError):
    """A step failed in an expected, explainable way (bad data, failed checks)."""


@dataclass
class Settings:
    users: int = 10_000
    workspaces: int | None = None
    seed: int = 42
    data_dir: str = field(default_factory=config.data_dir)
    # ingestion: full replace (optionally only events up to `through`) or a
    # partition replace of [start_date, end_date]
    through: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    full_refresh: bool = False
    database: str | None = None   # override POSTGRES_DB (e2e tests use a separate DB)

    def engine(self):
        return config.create_engine(self.database)

    def env(self):
        """Environment for subprocesses (dbt) pointing at the same database."""
        env = dict(os.environ)
        if self.database:
            env['POSTGRES_DB'] = self.database
        env.setdefault('POSTGRES_HOST', '127.0.0.1')
        return env


# ---------------------------------------------------------------- generate
def _generator_fingerprint():
    """Hash of the code that determines the generated data."""
    h = hashlib.sha256()
    for rel in ('scripts/generate_synthetic_data.py', 'experimentation/experiments.py',
                'experimentation/assignment.py'):
        with open(os.path.join(config.PROJECT_ROOT, rel), 'rb') as f:
            h.update(f.read().replace(b'\r\n', b'\n'))
    return h.hexdigest()[:16]


def generate(s: Settings):
    """Generate the dataset, unless data_dir already holds it (same params + code)."""
    sys.path.insert(0, os.path.join(config.PROJECT_ROOT, 'scripts'))
    import generate_synthetic_data as gen

    wanted = {'users': s.users, 'workspaces': s.workspaces or max(1, s.users // 10),
              'seed': s.seed, 'generator': _generator_fingerprint()}
    manifest_path = os.path.join(s.data_dir, MANIFEST)
    files = [f'{n}.parquet' for n in ('workspaces', 'users', 'events', 'agent_evaluations',
                                      'subscriptions', 'nps_responses')]
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
        if ({k: manifest.get(k) for k in wanted} == wanted
                and all(os.path.exists(os.path.join(s.data_dir, x)) for x in files)):
            return {'skipped': True, 'reason': 'data_dir already matches', **manifest['counts']}

    # Write to a staging directory, then move files into place, manifest last:
    # an interrupted run never leaves a manifest describing partial data.
    staging = os.path.join(s.data_dir, '.staging')
    shutil.rmtree(staging, ignore_errors=True)
    if os.path.exists(manifest_path):
        os.remove(manifest_path)
    counts = gen.generate_dataset(staging, users=s.users, workspaces=wanted['workspaces'],
                                  seed=s.seed, verbose=False)
    for name in files:
        os.replace(os.path.join(staging, name), os.path.join(s.data_dir, name))
    shutil.rmtree(staging, ignore_errors=True)
    with open(manifest_path, 'w') as f:
        json.dump({**wanted, 'counts': counts}, f, indent=2)
    return {'skipped': False, **counts}


# ---------------------------------------------------------------- assign
def assign(s: Settings):
    from experimentation.assignment import run
    from experimentation.experiments import EXPERIMENTS
    from experimentation.stat_tests import srm_check

    assignments, inserted = run(sorted(EXPERIMENTS), s.data_dir, persist=True,
                                engine=s.engine())
    per_experiment = {}
    for exp_id, grp in assignments.groupby('experiment_id'):
        counts = grp['variant'].value_counts().to_dict()
        srm = srm_check(counts.get('variant_0', 0), counts.get('variant_1', 0))
        per_experiment[exp_id] = {**counts, 'srm_p_value': srm['p_value']}
    return {'assignments': len(assignments), 'inserted': inserted,
            'already_present': len(assignments) - inserted, 'experiments': per_experiment}


# ---------------------------------------------------------------- ingest
def _dataset_id(data_dir):
    """Identity of the generated dataset in data_dir (from its manifest)."""
    path = os.path.join(data_dir, MANIFEST)
    if not os.path.exists(path):
        events = os.path.join(data_dir, 'events.parquet')
        return f'unmanaged-{os.path.getsize(events)}' if os.path.exists(events) else 'none'
    with open(path) as f:
        manifest = json.load(f)
    return hashlib.sha256(json.dumps({k: manifest[k] for k in
                                      ('users', 'workspaces', 'seed', 'generator')},
                                     sort_keys=True).encode()).hexdigest()[:16]


def ingest(s: Settings):
    """Load bronze. Dimension tables are always replaced. Events:
    - partition mode replaces [start_date, end_date] (incremental dbt picks it up);
    - full mode replaces everything, unless the same dataset is already loaded,
      and flags dbt for a full refresh when the history changed.
    """
    sys.path.insert(0, os.path.join(config.PROJECT_ROOT, 'scripts'))
    import ingest_events
    from sqlalchemy import text

    from pipeline import state

    engine = s.engine()
    events = ingest_events.EVENTS_TABLE
    base_id = _dataset_id(s.data_dir)
    ingest_events.create_tables(engine)
    with engine.connect() as conn:
        bronze_rows = conn.execute(text(f'SELECT COUNT(*) FROM {events}')).scalar()
    current = state.get(engine, events)

    if s.start_date:
        mode, wanted, skip = 'partition', f'{base_id}|partitioned', ()
        history_changed = current is None or not current['dataset_id'].startswith(base_id)
    else:
        mode = 'full_through' if s.through else 'full'
        wanted = f"{base_id}|through={s.through or 'all'}"
        already = (current is not None and current['dataset_id'] == wanted
                   and current['row_count'] == bronze_rows)
        skip = (events,) if already else ()
        history_changed = not already

    loaded = ingest_events.load_all_to_postgres(
        s.data_dir, through=s.through, start_date=s.start_date, end_date=s.end_date,
        engine=engine, skip_tables=skip)
    with engine.connect() as conn:
        bronze_rows = conn.execute(text(f'SELECT COUNT(*) FROM {events}')).scalar()
    state.record_load(engine, events, wanted, bronze_rows, history_changed)
    return {'mode': mode, 'events_skipped_already_loaded': bool(skip),
            'history_changed': history_changed, 'rows': loaded,
            'total_rows': sum(loaded.values()), 'bronze_events_rows': bronze_rows}


# ---------------------------------------------------------------- dbt
def dbt(s: Settings):
    from dbt.cli.main import dbtRunner

    from pipeline import state

    engine = s.engine()
    full_refresh = s.full_refresh or state.needs_full_refresh(engine)
    args = ['build', '--project-dir', config.dbt_dir(), '--profiles-dir', config.dbt_dir()]
    if full_refresh:
        args.append('--full-refresh')
    previous = {k: os.environ.get(k) for k in ('POSTGRES_DB', 'POSTGRES_HOST')}
    os.environ.update({k: v for k, v in s.env().items() if k in previous})
    try:
        res = dbtRunner().invoke(args)
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    if res.exception is not None:
        raise PipelineError(f'dbt could not run: {res.exception}')

    nodes = res.result.results
    models = [n for n in nodes if n.node.resource_type == 'model']
    tests = [n for n in nodes if n.node.resource_type == 'test']
    failed = [f'{n.node.name}: {n.status}' for n in nodes
              if str(n.status) in ('error', 'fail', 'skipped')]
    metrics = {
        'full_refresh': full_refresh,
        'full_refresh_reason': ('requested' if s.full_refresh else
                                'bronze history changed' if full_refresh else None),
        'models': len(models),
        'incremental_models': sum(1 for n in models
                                  if n.node.config.materialized == 'incremental'),
        'tests': len(tests),
        'tests_passed': sum(1 for n in tests if str(n.status) == 'pass'),
        'failed_nodes': failed,
        'slowest_models': sorted(
            ({'model': n.node.name, 'seconds': round(n.execution_time, 2)} for n in models),
            key=lambda x: -x['seconds'])[:5],
        'model_seconds_total': round(sum(n.execution_time for n in models), 2),
    }
    if not res.success:
        raise PipelineError(f"dbt build failed: {', '.join(failed) or 'see dbt log'}")
    state.clear_full_refresh(engine)
    return metrics


# ---------------------------------------------------------------- validation
def _validate(stage, s: Settings):
    from quality.validate import run_stage

    summary = run_stage(stage, config.database_url(s.database))
    metrics = {k: summary[k] for k in ('expectations', 'passed', 'critical_failures',
                                       'warnings', 'success')}
    metrics['validation_seconds'] = summary['duration_s']
    if not summary['success']:
        names = [f"{f['asset']}:{f['expectation']}" for f in summary['critical_failures']]
        raise PipelineError(f'{stage} data-quality checks failed: {", ".join(names)}')
    return metrics


def validate_bronze(s):
    return _validate('bronze', s)


def validate_gold(s):
    return _validate('gold', s)


def validate_analytics(s):
    return _validate('analytics', s)


# ---------------------------------------------------------------- analytics
def analytics(s: Settings):
    import pandas as pd

    from analytics import cohort_engine, health_scoring
    from experimentation import evaluate
    from experimentation.experiments import EXPERIMENTS

    engine = s.engine()
    scores = health_scoring.compute_health_scores(engine)
    if scores.empty:
        raise PipelineError('no workspaces to score: is gold.metrics_product_health empty?')
    persisted = health_scoring.persist_scores(engine, scores)
    snapshot = pd.Timestamp(scores['snapshot_date'].iloc[0])

    cells = cohort_engine.as_of(cohort_engine.load_retention(engine=engine), snapshot)
    retention = cohort_engine.summarize(cells, snapshot) if not cells.empty else None

    experiments = {}
    for experiment_id in sorted(EXPERIMENTS):
        result = evaluate.evaluate_experiment(engine, experiment_id)
        evaluate.persist_result(engine, result)
        p = result['primary_metric']
        experiments[experiment_id] = {
            'decision': result['decision'],
            'control_rate': p['control_rate'], 'treatment_rate': p['treatment_rate'],
            'relative_lift': p['relative_lift'], 'p_value': p['p_value'],
            'srm_p_value': result['srm_check']['p_value'],
        }
    return {
        'snapshot_date': str(snapshot.date()),
        'health': {'workspaces': persisted, 'tiers': health_scoring.tier_counts(scores)},
        'retention': retention,
        'experiments': experiments,
    }


STEPS = {name: globals()[name] for name in STEP_ORDER}


def settings_dict(s: Settings):
    return {k: v for k, v in asdict(s).items() if v is not None}
