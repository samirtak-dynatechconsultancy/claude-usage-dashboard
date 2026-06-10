-- 0007_backfill_turn_id.sql
-- One-time backfill: link messages to their turns via message_uuid → message_id.
--
-- Previously ingested messages may have turn_id = NULL because the linking
-- logic in /api/ingest was added after some data was already loaded (or
-- the message_uuid didn't match at insert time). This UPDATE joins on the
-- shared message identifier and fills in the FK.
--
-- Safe to re-run: only touches rows where turn_id IS NULL and a matching
-- turn exists. Idempotent.

UPDATE messages m
SET    turn_id = t.id
FROM   turns t
WHERE  m.message_uuid IS NOT NULL
  AND  m.message_uuid = t.message_id
  AND  m.turn_id IS NULL;
