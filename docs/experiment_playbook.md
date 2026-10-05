# Experiment Playbook

## How to Run an Experiment at ConnectHub

### 1. Define the Hypothesis
Write a clear hypothesis: "Changing X will increase Y by Z%"

### 2. Power Analysis
```python
from experimentation.power_analysis import required_sample_size, experiment_duration

n = required_sample_size(baseline_rate=0.32, mde=0.05)
days = experiment_duration(n, daily_traffic=5000)
print(f"Need {n:,} users per variant, ~{days} days")
```

### 3. Assign Variants
```python
from experimentation.assignment import assign_from_parquet

users = assign_from_parquet('data/users.parquet', 'exp_my_experiment')
```

### 4. Wait for Data Collection
Monitor daily via the Airflow DAG. Check for SRM issues weekly.

### 5. Evaluate Results
```python
from experimentation.evaluate import evaluate_from_parquet

results = evaluate_from_parquet(
    'data/experiment_assignments.parquet',
    'data/events.parquet',
    'exp_my_experiment'
)
```

### 6. Make a Decision
The evaluation pipeline returns one of:
- **SHIP** — Significant positive lift, guardrails passed
- **CONTINUE** — Promising but needs more data
- **REVERT** — No improvement or guardrail failure
- **HOLD** — SRM detected, investigate first

### Guardrail Metrics
Every experiment checks:
- Revenue per user (must not decrease)
- Session duration (directional check)
- Sample ratio mismatch (assignment integrity)
