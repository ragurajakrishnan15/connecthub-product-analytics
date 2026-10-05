"""
ConnectHub pipeline CLI.

    python -m pipeline run   [options]             all steps, each in its own process
    python -m pipeline step NAME [options]         one step (what Airflow runs)
    python -m pipeline fingerprint [--out F] [--compare F]
    python -m pipeline verify-incremental [options]

Options shared by run/step: --users N --seed S --data-dir DIR --through D
--start-date D --end-date D --full-refresh --database DB --run-id ID

Every step logs JSON lines (run_id, step, metrics, duration, peak memory) and
records a row in ops.pipeline_runs. Exit codes: 0 success, 1 step failed
(message on stderr and in the log), 2 usage error.
"""
import argparse
import json
import os
import subprocess
import sys
import time

from pipeline import config
from pipeline.log import get_logger, new_run_id, redact
from pipeline.resources import peak_rss_mb
from pipeline.steps import STEP_ORDER, STEPS, PipelineError, Settings, settings_dict

OPS_DDL = """
CREATE SCHEMA IF NOT EXISTS ops;
CREATE TABLE IF NOT EXISTS ops.pipeline_runs (
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    duration_s DOUBLE PRECISION,
    peak_memory_mb DOUBLE PRECISION,
    metrics JSONB,
    error TEXT,
    PRIMARY KEY (run_id, step)
)"""


def record_step(settings, run_id, step, status, started, duration, peak, metrics, error):
    """Upsert the step's row in ops.pipeline_runs (best effort: never fails the step)."""
    from sqlalchemy import text
    try:
        engine = settings.engine()
        with engine.begin() as conn:
            for stmt in OPS_DDL.split(';'):
                conn.execute(text(stmt))
            conn.execute(text(
                'INSERT INTO ops.pipeline_runs VALUES (:r, :s, :st, to_timestamp(:t), :d, :p, '
                'CAST(:m AS JSONB), :e) ON CONFLICT (run_id, step) DO UPDATE SET '
                'status = EXCLUDED.status, started_at = EXCLUDED.started_at, '
                'duration_s = EXCLUDED.duration_s, peak_memory_mb = EXCLUDED.peak_memory_mb, '
                'metrics = EXCLUDED.metrics, error = EXCLUDED.error'),
                {'r': run_id, 's': step, 'st': status, 't': started, 'd': duration, 'p': peak,
                 'm': json.dumps(metrics, default=str), 'e': error})
        engine.dispose()
    except Exception as exc:  # pragma: no cover - depends on the database being up
        get_logger(run_id, step).info('ops_record_skipped', reason=redact(exc))


def run_step(name, settings, run_id):
    """Run one step in this process. Returns 0 on success, 1 on failure."""
    log = get_logger(run_id, name)
    started = time.time()
    log.info('step.start', settings=settings_dict(settings))
    try:
        metrics = STEPS[name](settings)
        status, error, code = 'success', None, 0
    except PipelineError as exc:
        metrics, status, error, code = {}, 'failed', str(exc), 1
    except Exception as exc:
        metrics, status, error, code = {}, 'failed', f'{type(exc).__name__}: {exc}', 1
    duration = round(time.time() - started, 2)
    peak = peak_rss_mb()
    if code == 0:
        log.info('step.done', duration_s=duration, peak_memory_mb=peak, metrics=metrics)
    else:
        log.error('step.failed', duration_s=duration, peak_memory_mb=peak, error=redact(error))
        print(f'error: step {name} failed: {redact(error)}', file=sys.stderr)
    record_step(settings, run_id, name, status, started, duration, peak, metrics,
                redact(error) if error else None)
    return code


def run_all(settings, run_id, steps=STEP_ORDER, argv_settings=()):
    """Run steps in order, each as `python -m pipeline step` in a fresh process."""
    log = get_logger(run_id, 'run')
    started = time.time()
    log.info('run.start', steps=list(steps), settings=settings_dict(settings))
    env = dict(settings.env(), PIPELINE_RUN_ID=run_id)
    for name in steps:
        code = subprocess.call([sys.executable, '-m', 'pipeline', 'step', name,
                                *argv_settings], env=env, cwd=config.PROJECT_ROOT)
        if code != 0:
            log.error('run.failed', failed_step=name,
                      duration_s=round(time.time() - started, 2))
            return 1
    log.info('run.done', duration_s=round(time.time() - started, 2))
    return 0


def add_settings_args(p):
    p.add_argument('--users', type=int, default=10_000)
    p.add_argument('--workspaces', type=int)
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--data-dir', default=config.data_dir())
    p.add_argument('--through', help='Full load of events up to this date')
    p.add_argument('--start-date', help='Partition load: replace events from this date')
    p.add_argument('--end-date', help='Partition load end date (default: --start-date)')
    p.add_argument('--full-refresh', action='store_true', help='dbt build --full-refresh')
    p.add_argument('--database', help='Use this database instead of POSTGRES_DB')
    p.add_argument('--run-id', help='Defaults to $PIPELINE_RUN_ID or a new id')


def settings_from(args):
    return Settings(users=args.users, workspaces=args.workspaces, seed=args.seed,
                    data_dir=args.data_dir, through=args.through, start_date=args.start_date,
                    end_date=args.end_date, full_refresh=args.full_refresh,
                    database=args.database)


def settings_argv(args):
    """Re-serialize settings for child processes."""
    out = ['--users', str(args.users), '--seed', str(args.seed), '--data-dir', args.data_dir]
    for flag in ('workspaces', 'through', 'start_date', 'end_date', 'database'):
        value = getattr(args, flag)
        if value is not None:
            out += [f"--{flag.replace('_', '-')}", str(value)]
    if args.full_refresh:
        out.append('--full-refresh')
    return out


def verify_incremental(settings, run_id, last_day):
    """Load events through the day before `last_day` and build; then load `last_day`
    and build incrementally; fingerprint; full-refresh the same data; fingerprint.
    Returns (identical, details)."""
    from pipeline.fingerprint import diff, fingerprint
    import pandas as pd

    log = get_logger(run_id, 'verify_incremental')
    day_before = str((pd.Timestamp(last_day) - pd.Timedelta(days=1)).date())
    timings = {}

    def step(name, **overrides):
        s = Settings(**{**settings.__dict__, **overrides})
        t = time.time()
        if run_step(name, s, run_id) != 0:
            raise PipelineError(f'{name} failed during incremental verification')
        return round(time.time() - t, 2)

    step('ingest', through=day_before, start_date=None, end_date=None)
    timings['initial_full_build_s'] = step('dbt', full_refresh=True)
    step('ingest', through=None, start_date=last_day, end_date=last_day)
    timings['incremental_build_s'] = step('dbt', full_refresh=False)
    incremental = fingerprint(settings.engine())
    timings['full_refresh_build_s'] = step('dbt', full_refresh=True)
    full = fingerprint(settings.engine())
    differing = diff(incremental, full)
    details = {'relations_compared': len(full), 'differing': differing, **timings}
    log.info('verify_incremental.done', identical=not differing, **details)
    return not differing, details


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m pipeline', description=__doc__.split('\n')[1])
    sub = parser.add_subparsers(dest='command', required=True)
    p_run = sub.add_parser('run', help='Run all steps (or --steps a,b,c)')
    add_settings_args(p_run)
    p_run.add_argument('--steps', help=f'Comma-separated subset of {",".join(STEP_ORDER)}')
    p_step = sub.add_parser('step', help='Run one step in this process')
    p_step.add_argument('name', choices=STEP_ORDER)
    add_settings_args(p_step)
    p_fp = sub.add_parser('fingerprint', help='Fingerprint every warehouse relation')
    p_fp.add_argument('--database')
    p_fp.add_argument('--out', help='Write fingerprints to this JSON file')
    p_fp.add_argument('--compare', help='Compare with fingerprints in this JSON file')
    p_vi = sub.add_parser('verify-incremental',
                          help='Prove incremental dbt == full refresh on the same data')
    add_settings_args(p_vi)
    p_vi.add_argument('--last-day', default=config.DATA_END)
    args = parser.parse_args(argv)

    if args.command == 'fingerprint':
        from pipeline.fingerprint import diff, fingerprint
        fp = fingerprint(config.create_engine(args.database))
        if args.out:
            with open(args.out, 'w') as f:
                json.dump(fp, f, indent=1, sort_keys=True)
        if args.compare:
            with open(args.compare) as f:
                other = {k: tuple(v) for k, v in json.load(f).items()}
            differing = diff(other, fp)
            print(json.dumps({'relations': len(fp), 'identical': not differing,
                              'differing': differing}))
            return 0 if not differing else 1
        print(json.dumps({'relations': len(fp), 'rows': sum(v[0] for v in fp.values())}))
        return 0

    run_id = args.run_id or new_run_id()
    os.environ['PIPELINE_RUN_ID'] = run_id
    settings = settings_from(args)
    if args.start_date and args.through:
        parser.error('--through and --start-date are mutually exclusive')
    if args.command == 'step':
        return run_step(args.name, settings, run_id)
    if args.command == 'verify-incremental':
        try:
            identical, details = verify_incremental(settings, run_id, args.last_day)
        except PipelineError as exc:
            print(f'error: {exc}', file=sys.stderr)
            return 1
        print(json.dumps({'identical': identical, **details}))
        return 0 if identical else 1
    steps = args.steps.split(',') if args.steps else STEP_ORDER
    unknown = [s for s in steps if s not in STEP_ORDER]
    if unknown:
        parser.error(f'unknown steps: {unknown}')
    return run_all(settings, run_id, steps, settings_argv(args))


if __name__ == '__main__':
    sys.exit(main())
