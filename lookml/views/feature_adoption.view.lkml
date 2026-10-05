view: feature_adoption {
  sql_table_name: gold.fct_feature_adoption ;;

  dimension: feature_name {
    type: string
    sql: ${TABLE}.feature_name ;;
  }

  dimension: days_since_signup {
    type: number
    sql: ${TABLE}.days_since_signup ;;
  }

  measure: cumulative_adoption_pct {
    type: average
    sql: ${TABLE}.cumulative_adoption_pct ;;
    value_format_name: percent_1
  }

  measure: users_adopted {
    type: sum
    sql: ${TABLE}.users_adopted ;;
  }
}
