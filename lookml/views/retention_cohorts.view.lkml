view: retention_cohorts {
  sql_table_name: gold.fct_retention_cohorts ;;

  dimension: cohort_week {
    type: date_week
    sql: ${TABLE}.cohort_week ;;
  }

  dimension: weeks_since_signup {
    type: number
    sql: ${TABLE}.weeks_since_signup ;;
  }

  measure: retention_rate {
    type: average
    sql: ${TABLE}.retention_rate ;;
    value_format_name: percent_1
  }

  measure: cohort_size {
    type: sum
    sql: ${TABLE}.cohort_size ;;
  }

  measure: active_users {
    type: sum
    sql: ${TABLE}.active_users ;;
  }
}
