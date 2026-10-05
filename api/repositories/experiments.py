"""analytics.experiment_results (persisted evaluations) and the activation curve."""
from sqlalchemy import text

SOURCES = ('analytics.experiment_results', 'gold.fct_experiment_activation_curve')


def results(conn):
    rows = conn.execute(text(
        'SELECT experiment_id, decision, result_json FROM analytics.experiment_results')).all()
    return {r[0]: {'decision': r[1], 'result': r[2]} for r in rows}


def result(conn, experiment_id):
    row = conn.execute(text(
        'SELECT decision, result_json FROM analytics.experiment_results '
        'WHERE experiment_id = :e'), {'e': experiment_id}).first()
    return {'decision': row[0], 'result': row[1]} if row else None


def curve(conn, experiment_id):
    rows = conn.execute(text("""
        SELECT variant, day_since_signup, users_in_window, activated_cumulative,
               cumulative_activation_rate::float AS rate
        FROM gold.fct_experiment_activation_curve
        WHERE experiment_id = :e
        ORDER BY variant, day_since_signup"""), {'e': experiment_id}).mappings().all()
    return [dict(r) for r in rows]
