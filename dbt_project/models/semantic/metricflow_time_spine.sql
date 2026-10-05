-- Daily date spine required by dbt's semantic layer (MetricFlow).
SELECT day::DATE AS date_day
FROM generate_series('2024-01-01'::DATE, '2027-12-31'::DATE, INTERVAL '1 day') AS day
