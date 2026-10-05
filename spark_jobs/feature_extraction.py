"""
PySpark feature extraction: daily feature usage per user, workspace and
feature. Same grain and event -> feature mapping as dbt's int_feature_usage
(the mapping is shared: analytics/features.py), so its output can be checked
against the warehouse row for row (tests/test_spark_jobs.py).

    spark-submit spark_jobs/feature_extraction.py \
        --input data/events.parquet --output data/spark/feature_usage.parquet
"""
import argparse

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from analytics.features import FEATURE_MAP


def extract_features(events):
    """events -> (user_id, workspace_id, event_date, feature_name, usage_count, ...)."""
    mapping = F.create_map(*[F.lit(x) for pair in FEATURE_MAP.items() for x in pair])
    return (
        events
        .withColumn('feature_name', mapping[F.col('event_name')])
        .filter(F.col('feature_name').isNotNull())
        .groupBy('user_id', 'workspace_id', 'event_date', 'feature_name')
        .agg(F.count('*').alias('usage_count'),
             F.countDistinct('session_id').alias('session_count'),
             F.min('timestamp_utc').alias('first_use'),
             F.max('timestamp_utc').alias('last_use'))
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description='Daily feature usage per user')
    parser.add_argument('--input', default='data/events.parquet')
    parser.add_argument('--output', default='data/spark/feature_usage.parquet')
    args = parser.parse_args(argv)

    spark = (SparkSession.builder.appName('ConnectHub_FeatureExtraction')
             .config('spark.sql.session.timeZone', 'UTC').getOrCreate())
    usage = extract_features(spark.read.parquet(args.input))
    usage.write.mode('overwrite').partitionBy('event_date').parquet(args.output)
    print(f'Extracted {usage.count():,} feature usage rows -> {args.output}')
    spark.stop()


if __name__ == '__main__':
    main()
