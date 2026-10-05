{#
  Use the configured schema name as-is (staging / intermediate / gold / semantic)
  instead of dbt's default "<target_schema>_<custom_schema>" (e.g. public_gold).
  Application code, LookML and notebooks query these exact schema names.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
