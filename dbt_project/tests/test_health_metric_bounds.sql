-- Test: workspace health inputs stay within their valid ranges.
SELECT workspace_id, pct_ai_calls_automated, nps_score, mrr_usd
FROM {{ ref('metrics_product_health') }}
WHERE pct_ai_calls_automated NOT BETWEEN 0 AND 1
   OR nps_score NOT BETWEEN -100 AND 100
   OR mrr_usd < 0
   OR dau_over_seats_ratio < 0
