{#- Generic test: the combination of `columns` is unique (no dbt_utils dependency). -#}
{% test unique_combination_of_columns(model, columns) %}
SELECT {{ columns | join(', ') }}, COUNT(*) AS n
FROM {{ model }}
GROUP BY {{ columns | join(', ') }}
HAVING COUNT(*) > 1
{% endtest %}
