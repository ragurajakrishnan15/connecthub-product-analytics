"""
Run the data-quality contracts (quality/expectations.py) with Great Expectations.

    python -m quality.validate --stage bronze|gold|analytics|all [--json]

Uses an ephemeral GX context with a PostgreSQL datasource built from the
POSTGRES_* environment (nothing is written to disk). Exit codes: 0 all critical
checks passed, 1 at least one critical check failed or validation could not
run, 2 usage error.
"""
import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict

from quality.expectations import QUERIES, STAGES


def _context(url):
    os.environ.setdefault('GX_ANALYTICS_ENABLED', 'False')
    import great_expectations as gx
    for name in ('great_expectations', 'great_expectations.core'):
        logging.getLogger(name).setLevel(logging.ERROR)
    ctx = gx.get_context(mode='ephemeral')
    ctx.variables.progress_bars = {'globally': False}
    datasource = ctx.sources.add_postgres(
        name='warehouse', connection_string=url.render_as_string(hide_password=False))
    return ctx, datasource


def run_stage(stage, url=None, checks=None, queries=None):
    """Validate one stage. Returns a summary dict (never raises on failed checks)."""
    from great_expectations.core.expectation_configuration import ExpectationConfiguration
    from pipeline.config import database_url

    checks = STAGES[stage] if checks is None else checks
    queries = QUERIES if queries is None else queries
    start = time.perf_counter()
    ctx, datasource = _context(url or database_url())

    by_asset = defaultdict(list)
    for asset, expectation, kwargs, severity in checks:
        by_asset[asset].append((expectation, kwargs, severity))

    validations = []
    for i, (asset_name, expectations) in enumerate(by_asset.items()):
        if asset_name in queries:
            asset = datasource.add_query_asset(name=asset_name, query=queries[asset_name])
        else:
            schema, table = asset_name.split('.')
            asset = datasource.add_table_asset(name=asset_name, table_name=table,
                                               schema_name=schema)
        suite = ctx.add_or_update_expectation_suite(f'{stage}_{i}')
        for expectation, kwargs, severity in expectations:
            suite.add_expectation(ExpectationConfiguration(
                expectation, kwargs, meta={'severity': severity, 'asset': asset_name}))
        ctx.add_or_update_expectation_suite(expectation_suite=suite)
        validations.append({'batch_request': asset.build_batch_request(),
                            'expectation_suite_name': suite.expectation_suite_name})

    checkpoint = ctx.add_or_update_checkpoint(name=f'{stage}_checkpoint',
                                              validations=validations)
    result = checkpoint.run()

    failures = {'critical': [], 'warning': []}
    evaluated = 0
    for validation in result.list_validation_results():
        for r in validation.results:
            evaluated += 1
            if r.success:
                continue
            cfg = r.expectation_config
            detail = {k: v for k, v in r.result.items()
                      if k in ('unexpected_count', 'unexpected_percent', 'observed_value')}
            if r.exception_info and r.exception_info.get('raised_exception'):
                detail['error'] = str(r.exception_info.get('exception_message'))[:300]
            failures[cfg.meta.get('severity', 'critical')].append({
                'asset': cfg.meta.get('asset'),
                'expectation': cfg.expectation_type,
                'kwargs': {k: v for k, v in cfg.kwargs.items()
                           if k not in ('batch_id', 'value_set')},
                **detail,
            })
    return {
        'stage': stage,
        'expectations': evaluated,
        'passed': evaluated - len(failures['critical']) - len(failures['warning']),
        'critical_failures': failures['critical'],
        'warnings': failures['warning'],
        'success': not failures['critical'] and evaluated == len(checks),
        'duration_s': round(time.perf_counter() - start, 2),
    }


def describe(summary):
    head = (f"[{summary['stage']}] {summary['passed']}/{summary['expectations']} passed, "
            f"{len(summary['critical_failures'])} critical, {len(summary['warnings'])} warnings "
            f"({summary['duration_s']} s)")
    lines = [head]
    for level in ('critical_failures', 'warnings'):
        for f in summary[level]:
            lines.append(f"  {level.split('_')[0].upper()}: {f['asset']} {f['expectation']} "
                         f"{f['kwargs']} -> {({k: v for k, v in f.items() if k not in ('asset', 'expectation', 'kwargs')})}")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m quality.validate',
                                     description='Run data-quality checks for a pipeline stage.')
    parser.add_argument('--stage', choices=[*STAGES, 'all'], required=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    stages = list(STAGES) if args.stage == 'all' else [args.stage]
    ok = True
    for stage in stages:
        try:
            summary = run_stage(stage)
        except Exception as exc:
            from pipeline.log import redact
            print(f'error: validation of {stage} could not run: {redact(exc)}', file=sys.stderr)
            return 1
        print(json.dumps(summary, default=str) if args.json else describe(summary))
        ok &= summary['success']
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
