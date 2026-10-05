connection: "connecthub_warehouse"
include: "/views/**/*.view.lkml"

# Each gold table is its own explore: they are at different grains
# (workspace, cohort-week, feature-day, assignment) with no shared join key.

explore: product_health {
  label: "Product Health"
  description: "Workspace-level health inputs over the trailing 30 days (gold.metrics_product_health)"
}

explore: feature_adoption {
  label: "Feature Adoption"
  description: "Cumulative feature adoption by days since signup"
}

explore: experiments {
  label: "Experiments"
  description: "A/B test assignments and variant analysis"
}

explore: retention_cohorts {
  label: "Retention Cohorts"
  description: "Weekly retention cohort analysis"
}
