"""
Experiment registry: the single definition of every experiment.

Used by the assignment engine (who is in, which arm, when), by the synthetic
data generator (planted effects apply exactly to assigned users) and by the
pipeline (which experiments to assign and evaluate).

Eligibility:
- 'new_signups':     users who sign up inside [start, end]; assigned at signup.
- 'active_users':    users with any activity inside [start, end]; assigned at
                     their first event on or after `start`.
"""
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Experiment:
    experiment_id: str
    start: str               # inclusive, YYYY-MM-DD
    end: str                 # inclusive, YYYY-MM-DD
    eligibility: str         # 'new_signups' | 'active_users'
    traffic_pct: float = 1.0
    num_variants: int = 2
    salt: str = 'v1'         # change to reshuffle arms without changing traffic
    description: str = ''

    @property
    def start_ts(self):
        return pd.Timestamp(self.start)

    @property
    def end_ts(self):
        return pd.Timestamp(self.end) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)


EXPERIMENTS = {
    e.experiment_id: e for e in [
        Experiment(
            'exp_onboarding_v2', start='2025-01-01', end='2025-12-31',
            eligibility='new_signups', traffic_pct=1.0,
            description='New onboarding flow. Planted effect in the synthetic data: '
                        'variant_1 is 1.3x as likely to use an AI feature after the first call.'),
        Experiment(
            'exp_ai_summary_v1', start='2025-07-01', end='2025-12-31',
            eligibility='active_users', traffic_pct=0.5,
            description='A/A check: half of active users enter and no effect is planted. '
                        'Any single A/A run is significant 5% of the time at alpha=0.05; '
                        'tests check that rate across re-randomizations (salts).'),
    ]
}


def get_experiment(experiment_id):
    try:
        return EXPERIMENTS[experiment_id]
    except KeyError:
        known = ', '.join(sorted(EXPERIMENTS))
        raise KeyError(f'Unknown experiment {experiment_id!r}; known: {known}') from None
