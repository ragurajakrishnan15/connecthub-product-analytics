"""
Great Expectations Checkpoint for Events Data Quality
Runs between ingestion and dbt transformation stages.
"""

checkpoint_config = {
    "name": "events_checkpoint",
    "config_version": 1.0,
    "class_name": "SimpleCheckpoint",
    "run_name_template": "events_quality_%Y%m%d_%H%M%S",
    "validations": [
        {
            "batch_request": {
                "datasource_name": "connecthub_postgres",
                "data_connector_name": "default_inferred_data_connector",
                "data_asset_name": "bronze.events_raw",
            },
            "expectation_suite_name": "events_suite",
        }
    ],
}
