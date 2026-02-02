BEGIN;

CREATE SCHEMA IF NOT EXISTS marathon;
SET search_path TO marathon;

CREATE TABLE IF NOT EXISTS events (
  id           BIGSERIAL PRIMARY KEY,
  name         TEXT NOT NULL,
  event_date   DATE,
  photo_folder TEXT NOT NULL DEFAULT '',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS registrations (
  id           BIGSERIAL PRIMARY KEY,
  event_id     BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  email        TEXT NOT NULL,
  bib          TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(event_id, bib),
  UNIQUE(event_id, email)
);

CREATE TABLE IF NOT EXISTS photos (
  id           BIGSERIAL PRIMARY KEY,
  event_id     BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  file_path    TEXT NOT NULL,
  file_hash    TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(event_id, file_path)
);

CREATE TABLE IF NOT EXISTS detections (
  id           BIGSERIAL PRIMARY KEY,
  photo_id     BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
  x1           REAL NOT NULL,
  y1           REAL NOT NULL,
  x2           REAL NOT NULL,
  y2           REAL NOT NULL,
  yolo_conf    REAL NOT NULL,
  crop_path    TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_detections_photo_id ON detections(photo_id);

CREATE TABLE IF NOT EXISTS ocr_results (
  id           BIGSERIAL PRIMARY KEY,
  detection_id BIGINT NOT NULL REFERENCES detections(id) ON DELETE CASCADE,
  bib_pred     TEXT NOT NULL,
  ocr_conf     REAL NOT NULL,
  needs_review BOOLEAN NOT NULL DEFAULT FALSE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(detection_id)
);

CREATE INDEX IF NOT EXISTS idx_ocr_bib_pred ON ocr_results(bib_pred);

CREATE TABLE IF NOT EXISTS matches (
  id           BIGSERIAL PRIMARY KEY,
  event_id     BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  bib          TEXT NOT NULL,
  photo_id     BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
  match_score  REAL NOT NULL DEFAULT 0,
  status       TEXT NOT NULL DEFAULT 'auto',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(event_id, bib, photo_id)
);

CREATE INDEX IF NOT EXISTS idx_matches_event_bib ON matches(event_id, bib);

CREATE TABLE IF NOT EXISTS email_log (
  id           BIGSERIAL PRIMARY KEY,
  event_id     BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  email        TEXT NOT NULL,
  bib          TEXT,
  photo_count  INTEGER NOT NULL DEFAULT 0,
  status       TEXT NOT NULL DEFAULT 'queued',
  error        TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_email_log_email ON email_log(event_id, email);

COMMIT;
