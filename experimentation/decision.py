"""
Decision vocabulary shared by the evaluation (experimentation/evaluate.py) and
the analytics API. No numpy/scipy imports, so the API image can use it.
"""
DECISION_CODES = ('SHIP', 'CONTINUE', 'HOLD', 'REVERT')


def decision_code(decision):
    """'SHIP - Significant lift ...' -> 'SHIP'."""
    code = decision.split(' - ', 1)[0]
    if code not in DECISION_CODES:
        raise ValueError(f'unrecognized decision {decision!r}')
    return code


def guardrail_failed(guardrail):
    """A guardrail fails on a statistically significant decrease."""
    return bool(guardrail['significant'] and guardrail['absolute_diff'] < 0)
