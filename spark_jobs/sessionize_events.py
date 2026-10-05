"""
PySpark event sessionization: a new session starts after 30 minutes of
inactivity (or at a user's first event).

Not part of the production pipeline: dbt builds int_sessions from the
generator's session_id in PostgreSQL. This job is an independent check that
re-derives sessions from raw timestamps, and the path to take if event volume
outgrows a single PostgreSQL instance (see docs/architecture.md).

    spark-submit spark_jobs/sessionize_events.py \
        --input data/events.parquet --output data/spark/events_sessionized.parquet

--iceberg TABLE writes to an Iceberg table instead; it needs an Iceberg
catalog configured through spark-submit --conf (none is provided here).
"""
import argparse

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.window import Window

SESSION_GAP_SECONDS = 30 * 60


def sessionize(events):
    """Add spark_session_id, session_start/end, duration and event count."""
    user_window = Window.partitionBy('user_id').orderBy('timestamp_utc', 'event_id')
    sessionized = (
        events
        .withColumn('prev_ts', F.lag('timestamp_utc').over(user_window))
        .withColumn('gap_seconds',
                    F.unix_timestamp('timestamp_utc') - F.unix_timestamp('prev_ts'))
        .withColumn('new_session',
                    F.when(F.col('prev_ts').isNull(), 1)
                     .when(F.col('gap_seconds') > SESSION_GAP_SECONDS, 1)
                     .otherwise(0))
        .withColumn('session_number', F.sum('new_session').over(user_window))
        .withColumn('spark_session_id',
                    F.concat_ws('_', F.col('user_id'), F.col('session_number').cast('string')))
        .drop('prev_ts', 'gap_seconds', 'new_session', 'session_number')
    )
    session_window = Window.partitionBy('spark_session_id')
    return (
        sessionized
        .withColumn('session_start', F.min('timestamp_utc').over(session_window))
        .withColumn('session_end', F.max('timestamp_utc').over(session_window))
        .withColumn('session_duration_min',
                    (F.unix_timestamp('session_end') - F.unix_timestamp('session_start')) / 60)
        .withColumn('events_in_session', F.count('*').over(session_window))
    )


def create_spark(app_name='ConnectHub_Sessionization'):
    return (SparkSession.builder.appName(app_name)
            .config('spark.sql.session.timeZone', 'UTC')
            .getOrCreate())


def main(argv=None):
    parser = argparse.ArgumentParser(description='Sessionize events with a 30-minute gap')
    parser.add_argument('--input', default='data/events.parquet')
    parser.add_argument('--output', default='data/spark/events_sessionized.parquet')
    parser.add_argument('--iceberg', help='Write to this Iceberg table instead of --output')
    args = parser.parse_args(argv)

    spark = create_spark()
    result = sessionize(spark.read.parquet(args.input))
    if args.iceberg:
        result.writeTo(args.iceberg).createOrReplace()
        target = args.iceberg
    else:
        result.write.mode('overwrite').parquet(args.output)
        target = args.output
    sessions = result.select('spark_session_id').distinct().count()
    print(f'Sessionized {result.count():,} events into {sessions:,} sessions -> {target}')
    spark.stop()


if __name__ == '__main__':
    main()
