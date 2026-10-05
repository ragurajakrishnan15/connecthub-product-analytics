"""
PySpark Event Sessionization Pipeline
Reads raw events from Bronze layer, sessionizes with 30-min gap logic,
writes to Silver layer (Iceberg).
"""
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.window import Window
import sys

SESSION_GAP = 30 * 60  # 30 minutes in seconds


def create_spark_session():
    """Create Spark session with Iceberg catalog."""
    spark = SparkSession.builder \
        .appName('ConnectHub_Sessionization') \
        .config('spark.sql.catalog.lakehouse', 'org.apache.iceberg.spark.SparkCatalog') \
        .config('spark.sql.catalog.lakehouse.type', 'hadoop') \
        .config('spark.sql.catalog.lakehouse.warehouse', 's3://connecthub-lakehouse/') \
        .config('spark.sql.extensions', 'org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions') \
        .getOrCreate()
    return spark


def sessionize_events(spark, event_date):
    """Sessionize events for a given date using 30-minute inactivity gap."""

    # Read raw events from Iceberg Bronze layer
    raw_events = spark.read.format('iceberg') \
        .load('lakehouse.bronze.events_raw') \
        .filter(F.col('event_date') == event_date)

    # Define window for sessionization
    user_window = Window.partitionBy('user_id').orderBy('timestamp_utc')

    # Sessionize: detect gaps > 30 minutes
    sessionized = raw_events \
        .withColumn('prev_ts', F.lag('timestamp_utc').over(user_window)) \
        .withColumn('gap_seconds',
            F.unix_timestamp('timestamp_utc') - F.unix_timestamp('prev_ts')) \
        .withColumn('new_session',
            F.when(F.col('gap_seconds') > SESSION_GAP, 1)
             .when(F.col('prev_ts').isNull(), 1)
             .otherwise(0)) \
        .withColumn('session_id',
            F.concat(
                F.col('user_id'), F.lit('_'),
                F.sum('new_session').over(user_window)
            )) \
        .drop('prev_ts', 'gap_seconds', 'new_session')

    # Add session-level metrics
    session_window = Window.partitionBy('session_id')
    sessionized = sessionized \
        .withColumn('session_start', F.min('timestamp_utc').over(session_window)) \
        .withColumn('session_end', F.max('timestamp_utc').over(session_window)) \
        .withColumn('session_duration_min',
            (F.unix_timestamp('session_end') - F.unix_timestamp('session_start')) / 60) \
        .withColumn('events_in_session', F.count('*').over(session_window))

    # Write to Silver layer
    sessionized.writeTo('lakehouse.silver.events_sessionized') \
        .overwritePartitions()

    return sessionized


def sessionize_from_parquet(spark, input_path, output_path):
    """Alternative: sessionize from local parquet files (for local dev)."""

    raw_events = spark.read.parquet(input_path)

    user_window = Window.partitionBy('user_id').orderBy('timestamp_utc')

    sessionized = raw_events \
        .withColumn('prev_ts', F.lag('timestamp_utc').over(user_window)) \
        .withColumn('gap_seconds',
            F.unix_timestamp('timestamp_utc') - F.unix_timestamp('prev_ts')) \
        .withColumn('new_session',
            F.when(F.col('gap_seconds') > SESSION_GAP, 1)
             .when(F.col('prev_ts').isNull(), 1)
             .otherwise(0)) \
        .withColumn('session_id',
            F.concat(
                F.col('user_id'), F.lit('_'),
                F.sum('new_session').over(user_window)
            )) \
        .drop('prev_ts', 'gap_seconds', 'new_session')

    session_window = Window.partitionBy('session_id')
    sessionized = sessionized \
        .withColumn('session_start', F.min('timestamp_utc').over(session_window)) \
        .withColumn('session_end', F.max('timestamp_utc').over(session_window)) \
        .withColumn('session_duration_min',
            (F.unix_timestamp('session_end') - F.unix_timestamp('session_start')) / 60) \
        .withColumn('events_in_session', F.count('*').over(session_window))

    sessionized.write.mode('overwrite').parquet(output_path)
    print(f"Sessionized {sessionized.count():,} events → {output_path}")

    return sessionized


if __name__ == '__main__':
    spark = create_spark_session()

    if len(sys.argv) > 1 and sys.argv[1] == '--local':
        # Local dev mode: parquet → parquet
        sessionize_from_parquet(
            spark,
            input_path='data/events.parquet',
            output_path='data/events_sessionized.parquet'
        )
    else:
        # Production mode: Iceberg → Iceberg
        event_date = sys.argv[1] if len(sys.argv) > 1 else '2025-06-15'
        sessionize_events(spark, event_date)

    spark.stop()
