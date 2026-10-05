# ConnectHub Analytics Architecture

## Overview

Five-layer architecture processing 50M+ product events for a B2B SaaS communications platform.

## Layers

### 1. Ingestion
- **Kafka Topics** — Real-time product event streams
- **Kinesis Streams** — Clickstream and API call capture
- **S3 Raw Zone** — Landing zone for batch data (Parquet format)

### 2. Processing
- **PySpark** — Event sessionization (30-min gap logic), feature extraction
- **Airflow** — DAG orchestration with SLA monitoring and Slack alerting
- **Great Expectations** — Data quality contracts between pipeline stages

### 3. Lakehouse
- **Apache Iceberg** — Table format with time-travel, schema evolution, partition pruning
- **dbt** — SQL transformations on the local PostgreSQL warehouse: `bronze` (raw, loaded by `scripts/ingest_events.py`) → `staging` → `intermediate` → `gold`. Iceberg/S3 is not implemented yet.
- **Semantic Layer** — Governed metric definitions via dbt + LookML

### 4. Analytics
- **Cohort Engine** — Weekly retention matrix generation
- **A/B Test Engine** — Frequentist + Bayesian experiment evaluation
- **Health Scoring** — Workspace-level churn prediction
- **Agent Evals** — AI voice agent performance metrics

### 5. Serving
- **Hex** — Interactive SQL + Python notebooks for ad-hoc analysis
- **Looker** — Governed self-serve dashboards backed by LookML
- **Slack Alerts** — Automated metric anomaly detection

## Key Design Decisions

- **Iceberg over Delta Lake** — Time-travel for experiment snapshots, schema evolution as events change
- **dbt for transformations** — Version-controlled SQL, built-in testing, lineage tracking
- **Airflow for orchestration** — Dependency-aware scheduling, SLA monitoring
- **PySpark over Pandas** — Handles 50M+ events without memory issues
