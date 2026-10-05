{#-
  Lower bound of the incremental window: the latest date already in this model
  minus var('lookback_days'). Rows dated on/after it are recomputed; anything
  older is assumed final. Late data older than the lookback needs --full-refresh.
-#}
{% macro incremental_window_start(date_column) %}
    (SELECT COALESCE(MAX({{ date_column }}), DATE '1900-01-01') FROM {{ this }})
        - {{ var('lookback_days', 3) }}
{% endmacro %}
