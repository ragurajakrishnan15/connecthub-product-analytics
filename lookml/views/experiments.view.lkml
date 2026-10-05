view: experiments {
  sql_table_name: gold.fct_experiment_assignments ;;

  dimension: experiment_id {
    type: string
    sql: ${TABLE}.experiment_id ;;
  }

  dimension: user_id {
    type: string
    sql: ${TABLE}.user_id ;;
  }

  dimension: variant {
    type: string
    sql: ${TABLE}.variant ;;
  }

  dimension_group: assigned {
    type: time
    timeframes: [raw, date, week, month]
    sql: ${TABLE}.assigned_at ;;
  }

  measure: total_users {
    type: count_distinct
    sql: ${user_id} ;;
  }

  measure: variant_distribution {
    type: number
    sql: ${total_users} * 1.0 / NULLIF(SUM(${total_users}) OVER (), 0) ;;
    value_format_name: percent_2
  }
}
