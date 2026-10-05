"""
PySpark Feature Extraction
Computes daily feature usage aggregates per user per feature.
"""
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
import sys

FEATURE_MAPPING = {
    'ai_assist.used': 'ai_assist',
    'ai_assist.summary_generated': 'ai_assist',
    'ai_voice_agent.activated': 'ai_voice_agent',
    'ai_voice_agent.call_handled': 'ai_voice_agent',
    'call.started': 'voice_calls',
    'call.ended': 'voice_calls',
    'call.recorded': 'call_recording',
    'sms.sent': 'sms',
    'sms.received': 'sms',
    'whatsapp.sent': 'whatsapp',
    'whatsapp.received': 'whatsapp',
    'integration.installed': 'integrations',
    'integration.configured': 'integrations',
}


def extract_features(spark, input_path, output_path):
    """Extract daily feature usage from sessionized events."""

    events = spark.read.parquet(input_path)

    # Map event names to feature categories
    feature_mapping_expr = F.create_map(
        *[item for pair in FEATURE_MAPPING.items() for item in
          (F.lit(pair[0]), F.lit(pair[1]))]
    )

    feature_usage = events \
        .withColumn('feature_name',
            feature_mapping_expr[F.col('event_name')]) \
        .filter(F.col('feature_name').isNotNull()) \
        .groupBy('user_id', 'workspace_id', 'event_date', 'feature_name') \
        .agg(
            F.count('*').alias('event_count'),
            F.countDistinct('session_id').alias('session_count'),
            F.min('timestamp_utc').alias('first_use'),
            F.max('timestamp_utc').alias('last_use')
        )

    feature_usage.write.mode('overwrite') \
        .partitionBy('event_date') \
        .parquet(output_path)

    print(f"Extracted {feature_usage.count():,} feature usage records → {output_path}")
    return feature_usage


if __name__ == '__main__':
    spark = SparkSession.builder \
        .appName('ConnectHub_FeatureExtraction') \
        .getOrCreate()

    input_path = sys.argv[1] if len(sys.argv) > 1 else 'data/events_sessionized.parquet'
    output_path = sys.argv[2] if len(sys.argv) > 2 else 'data/feature_usage.parquet'

    extract_features(spark, input_path, output_path)
    spark.stop()
