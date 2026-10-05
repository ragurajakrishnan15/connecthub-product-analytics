view: product_health {
  # Built by dbt_project/models/gold/metrics_product_health.sql (one row per workspace).
  # Health score, NPS and revenue are not in the warehouse yet.
  sql_table_name: gold.metrics_product_health ;;

  dimension: workspace_id {
    type: string
    primary_key: yes
    sql: ${TABLE}.workspace_id ;;
  }

  dimension: workspace_name {
    type: string
    sql: ${TABLE}.workspace_name ;;
  }

  dimension: plan_tier {
    type: string
    sql: ${TABLE}.plan_tier ;;
  }

  dimension: seat_count {
    type: number
    sql: ${TABLE}.seat_count ;;
  }

  dimension_group: snapshot {
    type: time
    timeframes: [date, week, month]
    datatype: date
    sql: ${TABLE}.snapshot_date ;;
  }

  dimension: used_ai_feature_30d {
    type: yesno
    sql: ${TABLE}.used_ai_feature_30d ;;
  }

  measure: total_workspaces {
    type: count_distinct
    sql: ${workspace_id} ;;
  }

  measure: ai_active_workspaces {
    type: count_distinct
    sql: CASE WHEN ${used_ai_feature_30d} THEN ${workspace_id} END ;;
  }

  measure: ai_feature_adoption {
    type: number
    label: "AI Feature Adoption Rate"
    description: "Share of workspaces using at least one AI feature in the trailing 30 days"
    sql: ${ai_active_workspaces} * 1.0 / NULLIF(${total_workspaces}, 0) ;;
    value_format_name: percent_1
  }

  measure: avg_active_users_over_seats {
    type: average
    sql: ${TABLE}.dau_over_seats_ratio ;;
    value_format_name: percent_1
  }

  measure: avg_features_adopted {
    type: average
    sql: ${TABLE}.features_adopted_count ;;
    value_format_name: decimal_1
  }

  measure: avg_session_duration_minutes {
    type: average
    sql: ${TABLE}.avg_session_duration_minutes ;;
    value_format_name: decimal_1
  }

  measure: support_tickets_last_30d {
    type: sum
    sql: ${TABLE}.support_tickets_last_30d ;;
  }
}
