-- 0009_jwt_auth.sql
-- Add password_hash column to dashboard_users for JWT-based auth.
-- Replaces Supabase Auth magic-link flow.
-- Idempotent — safe to re-run.

ALTER TABLE dashboard_users ADD COLUMN IF NOT EXISTS password_hash TEXT;

-- Set a default password for the initial admin so they can log in
-- and then change it. SHA-256 of 'changeme123':
-- echo -n 'changeme123' | sha256sum → 0a8e6e1e5c...
-- IMPORTANT: change this password after first login via the invite flow.
UPDATE dashboard_users
SET password_hash = '494a715f7e9b4071aca61bac42ca858a309524e5864f0920030862a4ae7589be'
WHERE email = 'samir.tak@dynatechconsultancy.com'
  AND password_hash IS NULL;
