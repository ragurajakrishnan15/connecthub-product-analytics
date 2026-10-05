"""
Pipeline benchmark at a given scale, in a dedicated database.

    python scripts/benchmark.py --users 10000 [--repeats 3] [--data-dir DIR] [--keep]

1. Full pipeline run (`python -m pipeline run`): per-step duration and peak
   memory come from ops.pipeline_runs; PostgreSQL container memory is sampled
   with `docker stats` throughout.
2. dbt full refresh vs incremental on the same data: load events through the
   day before the last day, build, then repeatedly (a) replace the last day's
   partition and build incrementally, (b) build with --full-refresh.
   Medians over --repeats.

Prints one JSON document. The benchmark database is dropped afterwards unless
--keep is given.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text  # noqa: E402

from pipeline import config  # noqa: E402


class PostgresMemorySampler(threading.Thread):
    """Peak memory of the postgres container, sampled every 2 s via docker stats."""

    def __init__(self, container):
        super().__init__(daemon=True)
        self.container, self.peak_mb, self._halt = container, 0.0, threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                out = subprocess.run(['docker', 'stats', '--no-stream', '--format',
                                      '{{.MemUsage}}', self.container], capture_output=True,
                                     text=True, timeout=15).stdout.split('/')[0].strip()
                value = float(out[:-3])
                unit = out[-3:]
                mb = value * {'GiB': 1024, 'MiB': 1, 'KiB': 1 / 1024}.get(unit, 0)
                self.peak_mb = max(self.peak_mb, mb)
            except Exception:
                pass
            self._halt.wait(2)

    def stop(self):
        self._halt.set()
        self.join(timeout=20)
        return round(self.peak_mb, 1)


def pipeline(db, data_dir, users, *args):
    cmd = [sys.executable, '-m', 'pipeline', *args, '--database', db, '--users', str(users),
           '--data-dir', data_dir]
    t = time.perf_counter()
    proc = subprocess.run(cmd, cwd=config.PROJECT_ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f'failed: {" ".join(args)}\n{proc.stderr[-3000:]}')
    return round(time.perf_counter() - t, 2)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--users', type=int, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--data-dir', help='Defaults to data/bench/<users>')
    parser.add_argument('--last-day', default=config.DATA_END)
    parser.add_argument('--keep', action='store_true', help='Keep the benchmark database')
    args = parser.parse_args(argv)

    import pandas as pd
    data_dir = args.data_dir or os.path.join(config.PROJECT_ROOT, 'data', 'bench', str(args.users))
    os.makedirs(data_dir, exist_ok=True)
    db = f"{os.environ.get('POSTGRES_DB', 'connecthub_analytics')}_bench"
    admin = config.create_engine().execution_options(isolation_level='AUTOCOMMIT')
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{db}"'))
    container = subprocess.run(['docker', 'compose', 'ps', '-q', 'postgres'],
                               cwd=config.PROJECT_ROOT, capture_output=True,
                               text=True).stdout.strip()
    engine = config.create_engine(db)
    result = {'users': args.users}
    try:
        # 1. full pipeline
        run_id = f'bench-{args.users}-{int(time.time())}'
        sampler = PostgresMemorySampler(container)
        sampler.start()
        result['pipeline_wall_s'] = pipeline(db, data_dir, args.users, 'run', '--run-id', run_id)
        result['postgres_peak_memory_mb'] = sampler.stop()
        with engine.connect() as conn:
            rows = conn.execute(text(
                'SELECT step, duration_s, peak_memory_mb, metrics FROM ops.pipeline_runs '
                'WHERE run_id = :r ORDER BY started_at'), {'r': run_id}).all()
            result['warehouse_size_mb'] = round(conn.execute(text(
                'SELECT pg_database_size(current_database())')).scalar() / 2**20, 1)
            result['events'] = conn.execute(text('SELECT COUNT(*) FROM bronze.events_raw')).scalar()
        result['steps'] = {r.step: {'duration_s': r.duration_s, 'peak_memory_mb': r.peak_memory_mb}
                           for r in rows}
        result['steps_total_s'] = round(sum(r.duration_s for r in rows), 2)

        # 2. incremental vs full refresh on identical data
        day_before = str((pd.Timestamp(args.last_day) - pd.Timedelta(days=1)).date())
        pipeline(db, data_dir, args.users, 'step', 'ingest', '--through', day_before)
        pipeline(db, data_dir, args.users, 'step', 'dbt', '--full-refresh')
        incremental, full = [], []
        for _ in range(args.repeats):
            pipeline(db, data_dir, args.users, 'step', 'ingest', '--start-date', args.last_day)
            incremental.append(pipeline(db, data_dir, args.users, 'step', 'dbt'))
            full.append(pipeline(db, data_dir, args.users, 'step', 'dbt', '--full-refresh'))
        result['dbt_incremental_s'] = {'median': statistics.median(incremental),
                                       'runs': incremental}
        result['dbt_full_refresh_s'] = {'median': statistics.median(full), 'runs': full}
        result['dbt_speedup'] = round(statistics.median(full) / statistics.median(incremental), 2)
    finally:
        engine.dispose()
        if not args.keep:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
        admin.dispose()
    print(json.dumps(result, indent=1, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
