-- The people who use this system are the company's own staff, not outside customers.
-- So every user is a staff member unless that is taken away: they see and may answer
-- every open challenge. `admin set-staff --email ... --staff false` restricts one user
-- (for example a program's account) to the challenges of its own lookups.
ALTER TABLE users ALTER COLUMN is_staff SET DEFAULT true;
UPDATE users SET is_staff = true;
