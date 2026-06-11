CREATE OR REPLACE FUNCTION recompute_session_totals(target_session_id UUID)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    chosen_model TEXT;
BEGIN
    SELECT model INTO chosen_model
    FROM (
        SELECT
            model,
            COUNT(*) AS turn_count,
            CASE
                WHEN LOWER(model) LIKE '%opus%'   THEN 3
                WHEN LOWER(model) LIKE '%sonnet%' THEN 2
                WHEN LOWER(model) LIKE '%haiku%'  THEN 1
                ELSE 0
            END AS priority
        FROM turns
        WHERE session_id = target_session_id
          AND model IS NOT NULL
          AND model != ''
        GROUP BY model
    ) AS m
    ORDER BY priority DESC, turn_count DESC
    LIMIT 1;

    UPDATE sessions
    SET
        total_input_tokens     = COALESCE((SELECT SUM(input_tokens)          FROM turns WHERE session_id = target_session_id), 0),
        total_output_tokens    = COALESCE((SELECT SUM(output_tokens)         FROM turns WHERE session_id = target_session_id), 0),
        total_cache_read       = COALESCE((SELECT SUM(cache_read_tokens)     FROM turns WHERE session_id = target_session_id), 0),
        total_cache_creation   = COALESCE((SELECT SUM(cache_creation_tokens) FROM turns WHERE session_id = target_session_id), 0),
        turn_count             = COALESCE((SELECT COUNT(*)                   FROM turns WHERE session_id = target_session_id), 0),
        first_timestamp        = COALESCE((SELECT MIN(timestamp)             FROM turns WHERE session_id = target_session_id), first_timestamp),
        last_timestamp         = COALESCE((SELECT MAX(timestamp)             FROM turns WHERE session_id = target_session_id), last_timestamp),
        model                  = COALESCE(chosen_model, model),
        updated_at             = now()
    WHERE id = target_session_id;
END;
$$;
