"""
Full Experiment Evaluation Pipeline
End-to-end evaluation with primary metrics, Bayesian analysis, and guardrails.

Both entry points evaluate the same per-user metrics (gold.fct_experiment_user_metrics):
- primary   activated_14d: placed a call, used an AI feature and invited a
            teammate within 14 days of signup
- guardrail avg_session_minutes_14d: must not drop significantly
- guardrail revenue_60d: seat revenue billed for the user within 60 days of
            signup; must not drop significantly
Each metric only uses users whose observation window is complete.

CLI:
    python -m experimentation.evaluate --experiment-id exp_onboarding_v2 [--experiment-id ...]
        [--all] [--source warehouse|parquet] [--data-dir data] [--persist] [--json]
        [--fail-on-srm]
--persist replaces the experiment's row in analytics.experiment_results.
Exit codes: 0 ok, 1 error (unknown experiment, no data), 2 usage error,
3 sample ratio mismatch detected with --fail-on-srm.
"""
import argparse
import json
import os
import sys

import pandas as pd
from experimentation.stat_tests import z_test_proportions, t_test_continuous, srm_check
from experimentation.bayesian_ab import bayesian_ab_test
from experimentation.decision import DECISION_CODES, decision_code, guardrail_failed  # noqa: F401

CONTROL, TREATMENT = 'variant_0', 'variant_1'
AI_EVENTS = ('ai_assist.used', 'ai_voice_agent.activated')


def evaluate_experiment(conn, experiment_id):
    """Evaluate an experiment from the warehouse (gold.fct_experiment_user_metrics)."""
    query = """
    SELECT variant, user_id, activated_14d, avg_session_minutes_14d, revenue_60d,
           window_14d_complete, window_60d_complete
    FROM gold.fct_experiment_user_metrics
    WHERE experiment_id = %(exp_id)s
    """
    users = pd.read_sql(query, conn, params={'exp_id': experiment_id})
    return evaluate_user_metrics(users, experiment_id)


def user_metrics_from_parquet(assignments_path, events_path, users_path,
                              subscriptions_path, experiment_id):
    """Per-user experiment metrics from parquet; mirrors fct_experiment_user_metrics."""
    assignments = pd.read_parquet(assignments_path, columns=['experiment_id', 'user_id', 'variant'])
    assignments = assignments[assignments['experiment_id'] == experiment_id]
    users = pd.read_parquet(users_path, columns=['user_id', 'signup_date'])
    users['signup_date'] = pd.to_datetime(users['signup_date'])
    events = pd.read_parquet(events_path, columns=[
        'event_id', 'user_id', 'workspace_id', 'event_name', 'timestamp_utc',
        'event_date', 'session_id']).drop_duplicates('event_id')
    events['event_date'] = pd.to_datetime(events['event_date'])
    last_event_date = events['event_date'].max()

    ev = events.merge(users, on='user_id')
    days = (ev['event_date'] - ev['signup_date']).dt.days

    in_14d = ev[days <= 14]
    def reached(names):
        return in_14d.loc[in_14d['event_name'].isin(names), 'user_id'].unique()
    activated = (set(reached(['call.started'])) & set(reached(AI_EVENTS))
                 & set(reached(['team.member_invited'])))

    sessions = ev.groupby(['session_id', 'user_id', 'workspace_id']).agg(
        start=('timestamp_utc', 'min'), end=('timestamp_utc', 'max'),
        session_date=('event_date', 'min'), signup_date=('signup_date', 'first')).reset_index()
    sessions['minutes'] = (sessions['end'] - sessions['start']).dt.total_seconds() / 60
    sessions = sessions[(sessions['session_date'] - sessions['signup_date']).dt.days <= 14]
    session_stats = sessions.groupby('user_id')['minutes'].agg(['size', 'mean'])

    subs = pd.read_parquet(subscriptions_path, columns=['workspace_id', 'month_start',
                                                        'seat_price_usd'])
    subs['month_start'] = pd.to_datetime(subs['month_start'])
    months = ev.loc[days <= 60, ['user_id', 'workspace_id', 'event_date']]
    months = months.assign(month_start=months['event_date'].dt.to_period('M').dt.start_time)
    months = months[['user_id', 'workspace_id', 'month_start']].drop_duplicates()
    revenue = months.merge(subs, on=['workspace_id', 'month_start']) \
        .groupby('user_id')['seat_price_usd'].sum()

    out = assignments.merge(users, on='user_id')
    out['activated_14d'] = out['user_id'].isin(activated).astype(int)
    out['sessions_14d'] = out['user_id'].map(session_stats['size']).fillna(0).astype(int)
    out['avg_session_minutes_14d'] = out['user_id'].map(session_stats['mean']).round(4)
    out['revenue_60d'] = out['user_id'].map(revenue).fillna(0.0)
    out['window_14d_complete'] = out['signup_date'] + pd.Timedelta(days=14) <= last_event_date
    out['window_60d_complete'] = out['signup_date'] + pd.Timedelta(days=60) <= last_event_date
    return out


def evaluate_from_parquet(assignments_path, events_path, experiment_id,
                          users_path=None, subscriptions_path=None):
    """Evaluate an experiment from local parquet files.

    users.parquet and subscriptions.parquet default to the events file's directory.
    """
    data_dir = os.path.dirname(events_path)
    users = user_metrics_from_parquet(
        assignments_path, events_path,
        users_path or os.path.join(data_dir, 'users.parquet'),
        subscriptions_path or os.path.join(data_dir, 'subscriptions.parquet'),
        experiment_id)
    return evaluate_user_metrics(users, experiment_id)


def evaluate_user_metrics(users, experiment_id):
    """Core evaluation over one row per user (variant + metric columns)."""
    control = users[users['variant'] == CONTROL]
    treatment = users[users['variant'] == TREATMENT]

    srm = srm_check(len(control), len(treatment))

    # Primary metric: 14-day activation (frequentist + Bayesian)
    c14 = control[control['window_14d_complete'].astype(bool)]
    t14 = treatment[treatment['window_14d_complete'].astype(bool)]
    counts = (int(c14['activated_14d'].sum()), len(c14),
              int(t14['activated_14d'].sum()), len(t14))
    primary = z_test_proportions(*counts)
    bayesian = bayesian_ab_test(*counts)

    # Guardrails: must not decrease significantly
    guardrail_session = t_test_continuous(
        c14['avg_session_minutes_14d'].dropna().astype(float).to_numpy(),
        t14['avg_session_minutes_14d'].dropna().astype(float).to_numpy())
    c60 = control[control['window_60d_complete'].astype(bool)]
    t60 = treatment[treatment['window_60d_complete'].astype(bool)]
    guardrail_revenue = t_test_continuous(c60['revenue_60d'].astype(float).to_numpy(),
                                          t60['revenue_60d'].astype(float).to_numpy())
    decision = _make_decision(primary, bayesian, srm, guardrail_revenue, guardrail_session)

    return {
        'experiment_id': experiment_id,
        'srm_check': srm,
        'primary_metric': primary,
        'bayesian_analysis': bayesian,
        'guardrail_session_duration': guardrail_session,
        'guardrail_revenue': guardrail_revenue,
        'sample_sizes': {
            'control': len(control),
            'treatment': len(treatment),
            'control_14d_window': len(c14),
            'treatment_14d_window': len(t14),
            'control_60d_window': len(c60),
            'treatment_60d_window': len(t60),
        },
        'decision': decision,
        # The verdict alone (SHIP / CONTINUE / HOLD / REVERT), for machine consumers.
        'decision_code': decision_code(decision),
    }


_guardrail_failed = guardrail_failed


def _make_decision(primary, bayesian, srm, guardrail_revenue, guardrail_session):
    """Automated decision recommendation."""
    if srm['srm_detected']:
        return 'HOLD - Sample ratio mismatch detected, investigate assignment logic'
    if _guardrail_failed(guardrail_revenue):
        return 'REVERT - Revenue guardrail failed (significant decrease)'
    if _guardrail_failed(guardrail_session):
        return 'REVERT - Session duration guardrail failed (significant decrease)'
    if primary['significant'] and primary['relative_lift'] > 0:
        return f"SHIP - Significant lift of {primary['relative_lift']:.1%} (p={primary['p_value']:.4f})"
    if bayesian['prob_treatment_better'] > 0.95:
        return f"SHIP - Bayesian probability {bayesian['prob_treatment_better']:.1%} treatment is better"
    if bayesian['prob_treatment_better'] > 0.80:
        return 'CONTINUE - Promising but needs more data'
    return 'REVERT - No significant improvement detected'


RESULTS_TABLE = 'analytics.experiment_results'


def evaluate_source(experiment_id, source='warehouse', data_dir='data', engine=None):
    if source == 'parquet':
        return evaluate_from_parquet(os.path.join(data_dir, 'experiment_assignments.parquet'),
                                     os.path.join(data_dir, 'events.parquet'), experiment_id)
    return evaluate_experiment(engine, experiment_id)


def persist_result(engine, result):
    """Replace the experiment's row in analytics.experiment_results (idempotent)."""
    from sqlalchemy import text
    p, s = result['primary_metric'], result['srm_check']
    row = {
        'experiment_id': result['experiment_id'],
        'decision': result['decision'],
        'control_users': result['sample_sizes']['control'],
        'treatment_users': result['sample_sizes']['treatment'],
        'control_rate': p['control_rate'], 'treatment_rate': p['treatment_rate'],
        'relative_lift': p['relative_lift'], 'p_value': p['p_value'],
        'srm_p_value': s['p_value'], 'srm_detected': s['srm_detected'],
        'guardrail_session_p_value': result['guardrail_session_duration']['p_value'],
        'guardrail_revenue_p_value': result['guardrail_revenue']['p_value'],
        'result_json': json.dumps(result, sort_keys=True),
    }
    with engine.begin() as conn:
        conn.execute(text('CREATE SCHEMA IF NOT EXISTS analytics'))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} (
                experiment_id TEXT PRIMARY KEY, decision TEXT NOT NULL,
                control_users INTEGER, treatment_users INTEGER,
                control_rate DOUBLE PRECISION, treatment_rate DOUBLE PRECISION,
                relative_lift DOUBLE PRECISION, p_value DOUBLE PRECISION,
                srm_p_value DOUBLE PRECISION, srm_detected BOOLEAN,
                guardrail_session_p_value DOUBLE PRECISION,
                guardrail_revenue_p_value DOUBLE PRECISION,
                result_json JSONB NOT NULL
            )"""))
        conn.execute(text(f'DELETE FROM {RESULTS_TABLE} WHERE experiment_id = :e'),
                     {'e': row['experiment_id']})
        cols = ', '.join(row)
        conn.execute(text(f'INSERT INTO {RESULTS_TABLE} ({cols}) VALUES '
                          f"({', '.join(':' + c for c in row)})"), row)


def describe(result):
    p, n = result['primary_metric'], result['sample_sizes']
    return (f"{result['experiment_id']}: {result['decision']} | activation "
            f"{p['control_rate']:.2%} -> {p['treatment_rate']:.2%} "
            f"(lift {p['relative_lift']:+.1%}, p={p['p_value']:.4g}) | n={n['control']:,}/"
            f"{n['treatment']:,} | SRM p={result['srm_check']['p_value']:.4f} | guardrails: "
            f"{_guardrail_text('session', result['guardrail_session_duration'])}, "
            f"{_guardrail_text('revenue', result['guardrail_revenue'])}")


def _guardrail_text(name, g):
    return f"{name} {g['relative_lift']:+.1%} (p={g['p_value']:.3f})"


def main(argv=None):
    from experimentation.experiments import EXPERIMENTS, get_experiment
    parser = argparse.ArgumentParser(prog='python -m experimentation.evaluate',
                                     description='Evaluate A/B experiments.')
    parser.add_argument('--experiment-id', action='append', dest='experiments')
    parser.add_argument('--all', action='store_true', help='Evaluate every registered experiment')
    parser.add_argument('--source', choices=['warehouse', 'parquet'], default='warehouse')
    parser.add_argument('--data-dir', default='data')
    parser.add_argument('--persist', action='store_true',
                        help=f'Replace the result rows in {RESULTS_TABLE}')
    parser.add_argument('--json', action='store_true', help='Print full results as JSON lines')
    parser.add_argument('--fail-on-srm', action='store_true',
                        help='Exit 3 if any experiment shows a sample ratio mismatch')
    parser.add_argument('--date', help=argparse.SUPPRESS)  # accepted for scheduler compatibility
    args = parser.parse_args(argv)
    if args.all == bool(args.experiments):
        parser.error('pass --experiment-id (repeatable) or --all')
    experiments = sorted(EXPERIMENTS) if args.all else args.experiments
    try:
        for e in experiments:
            get_experiment(e)
    except KeyError as exc:
        print(f'error: {exc.args[0]}', file=sys.stderr)
        return 1

    engine = None
    if args.source == 'warehouse' or args.persist:
        from pipeline.config import create_engine
        engine = create_engine()
    srm_failed = False
    for experiment_id in experiments:
        try:
            result = evaluate_source(experiment_id, args.source, args.data_dir, engine)
        except (IndexError, ValueError, ZeroDivisionError, FileNotFoundError) as exc:
            print(f'error: {experiment_id}: no evaluable data ({type(exc).__name__}: {exc})',
                  file=sys.stderr)
            return 1
        if args.persist:
            persist_result(engine, result)
        print(json.dumps(result, default=str) if args.json else describe(result))
        srm_failed |= result['srm_check']['srm_detected']
    if srm_failed and args.fail_on_srm:
        print('error: sample ratio mismatch detected', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
