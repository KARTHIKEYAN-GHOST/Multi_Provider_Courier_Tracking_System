-- Phase 10: staff answer challenges, and who answered is recorded.

-- A staff user sees and may answer every open challenge (one-time codes, CAPTCHAs), not
-- only those of their own lookups. Set with: admin set-staff --email ... --staff true
ALTER TABLE users ADD COLUMN is_staff boolean NOT NULL DEFAULT false;

-- The user who sent the answer: the assigned user or a staff member.
ALTER TABLE human_interventions ADD COLUMN answered_by uuid REFERENCES users (id) ON DELETE SET NULL;

-- Open challenges in the order staff work through them.
CREATE INDEX ix_human_interventions_pending ON human_interventions (created_at, id) WHERE status = 'pending';
