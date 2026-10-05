# Semantic Layer Guide

## How to Add a New Metric to the ConnectHub Semantic Layer

### Overview
ConnectHub uses a dual semantic layer:
- **dbt metrics** — governed metric definitions consumed by the analytics Python modules
- **Looker LookML** — governed dimensions and measures for self-serve dashboards

### Adding a New Metric

#### Step 1: Define in dbt
Metrics use the dbt semantic layer (MetricFlow) spec in
`dbt_project/models/semantic/metrics_product_health.yml`. A measure lives on a
`semantic_models` entry; a `simple` metric exposes a measure; a `ratio` metric
divides two metrics:

```yaml
semantic_models:
  - name: my_model
    model: ref('some_gold_or_intermediate_model')
    defaults:
      agg_time_dimension: event_date
    entities:
      - name: user
        type: primary
        expr: user_id
    dimensions:
      - name: event_date
        type: time
        type_params:
          time_granularity: day
    measures:
      - name: numerator_measure
        agg: sum
        expr: some_flag
      - name: denominator_measure
        agg: count
        expr: user_id

metrics:
  - name: numerator_metric
    type: simple
    type_params:
      measure: numerator_measure
  - name: denominator_metric
    type: simple
    type_params:
      measure: denominator_measure
  - name: my_new_metric
    label: "My New Metric"
    description: "Clear description of what this measures and how"
    type: ratio
    type_params:
      numerator: numerator_metric
      denominator: denominator_metric
```

Run `dbt parse` to validate the definitions.

#### Step 2: Add to LookML
Create or update the relevant view in `lookml/views/`:

```lookml
measure: my_new_metric {
  type: number
  label: "My New Metric"
  description: "Same description as dbt"
  sql: ${numerator} * 1.0 / NULLIF(${denominator}, 0) ;;
  value_format_name: percent_1
}
```

#### Step 3: Test
- Run `dbt test` to validate the metric
- Use `spectacles` to validate LookML: `spectacles sql --project connecthub`

#### Step 4: Document
Add the metric to this guide with:
- Name and description
- Business question it answers
- Source tables
- Owner (team responsible)

### Current Metrics

| Metric | Description | Owner |
|--------|-------------|-------|
| activation_rate_14d | % of new users completing all 4 milestones in 14 days | Product |
| weekly_retention_rate | % of cohort active in week N | Product |
| ai_feature_adoption | % of workspaces using AI in trailing 30 days | AI Team |
| revenue_per_workspace | Not defined yet: no revenue data in the warehouse | Finance |
| health_score | Composite churn risk score (0-100), computed in `analytics/health_scoring.py` from `gold.metrics_product_health` | CS Team |
