-- Staging: NPS survey responses (score 0-10) with promoter/detractor classification
SELECT
    response_id,
    user_id,
    workspace_id,
    response_date::DATE AS response_date,
    score,
    CASE
        WHEN score >= 9 THEN 'promoter'
        WHEN score >= 7 THEN 'passive'
        ELSE 'detractor'
    END AS nps_category
FROM {{ source('bronze', 'nps_responses') }}
WHERE response_id IS NOT NULL
  AND score BETWEEN 0 AND 10
