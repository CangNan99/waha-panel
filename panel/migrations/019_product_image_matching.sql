-- Product-image matching metadata migration.
--
-- SQLite does not support ADD COLUMN IF NOT EXISTS.  The application applies
-- the three columns idempotently with _add_column_if_missing (and the
-- standalone ProductImageService performs the same check) after this marker
-- migration is read.
SELECT 1;
