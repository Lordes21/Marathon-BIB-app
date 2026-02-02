# CLAUDE.md - Marathon BIB Photo Delivery App

## Project Overview

A full-stack computer vision system that detects marathon runner bib numbers in photos and delivers matched images to participants. The pipeline: ingest photos → YOLO bib detection → PARSeq OCR → match to registrations → email delivery with OTP authentication.

## Tech Stack

- **Runtime:** Python 3.10
- **Web framework:** Flask 3.1.2 (Jinja2 templates, no JSON API)
- **Production server:** Gunicorn
- **Database:** PostgreSQL 16 (direct psycopg2, no ORM)
- **Object storage:** MinIO (S3-compatible, accessed via boto3)
- **ML models:** Ultralytics YOLOv8 (bib detection), PARSeq (OCR), EasyOCR (fallback)
- **ML framework:** PyTorch 2.9.1, PyTorch Lightning
- **Frontend:** Vanilla HTML/CSS/JS, dark green theme, no build tooling

## Repository Structure

```
app.py                  # Flask web app (main entry point, routes, auth, email)
storage.py              # S3/MinIO client configuration
ingest_photos.py        # Upload local photos to MinIO + record in DB
run_yolo.py             # Batch YOLO bib detection pipeline
run_parseq.py           # Batch PARSeq OCR pipeline
make_csv.py             # Convert YOLO detections to CSV for OCR

templates/              # Jinja2 templates (index, verify, done)
static/                 # CSS + JS (style.css, app.js with lightbox gallery)
db/                     # schema_v1.sql (PostgreSQL schema), docker-compose.yml
models/                 # Trained weights: best.pt (YOLO), last.ckpt (PARSeq)
src/                    # Utility scripts (ocr_bib_crops.py - EasyOCR alternative)
parseq_recognizer/      # PARSeq custom inference/training tools
```

## Running the Application

### Docker (production)

```bash
docker-compose up --build
```

Three services: PostgreSQL (:5433), MinIO (:9000/:9001), Flask app (:5000).

### Local development

```bash
python app.py                # Flask dev server on :5000
```

### ML pipelines (run sequentially)

```bash
python ingest_photos.py      # Upload photos to MinIO
python run_yolo.py           # Detect bibs in photos
python run_parseq.py         # OCR detected bib crops
```

## Database

PostgreSQL with `marathon` schema. 8 tables: `events`, `registrations`, `photos`, `detections`, `ocr_results`, `matches`, `email_log`, `otp_requests`.

- Schema defined in `db/schema_v1.sql` (must be loaded manually for fresh setup)
- `app.py:init_db()` auto-creates `marathon` schema and `otp_requests` table on startup
- Connection helper: `pg()` returns a psycopg2 connection
- All queries use raw SQL with parameterized queries

### Key relationships

- `photos` → `detections` → `ocr_results` (cascade deletes)
- `registrations` maps email → bib per event
- `matches` links bib → photo per event

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `EVENT_ID` | Active event ID | `1` |
| `PG_SCHEMA` | Database schema | `marathon` |
| `SECRET_KEY` | Flask session + token signing | (required) |
| `PG_HOST/PORT/DB/USER/PASS` | PostgreSQL connection | See docker-compose.yml |
| `S3_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET` | MinIO connection | See docker-compose.yml |
| `SMTP_HOST/PORT/USER/PASS/FROM` | Email sending | (required for OTP) |

## Flask Routes

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/` | Landing page (email entry) |
| POST | `/request_code` | Send OTP to email |
| POST | `/verify_and_send` | Verify OTP, send download link |
| GET | `/download/<token>` | Download ZIP of matched photos (24hr token) |

## Code Conventions

- **Functions/variables:** `snake_case`
- **Constants:** `UPPER_SNAKE_CASE`
- **DB tables:** `lowercase`
- **No type hints** used in existing code
- **No ORM** — raw parameterized SQL everywhere
- **No structured logging** — uses `print()` statements
- **Environment config pattern:** `os.environ.get("VAR", "default")`
- **DB pattern:** `with pg() as conn: with conn.cursor() as cur: ...`
- **S3 key scheme:** `events/{event_id}/photos/{sha256_hash}{ext}` and `events/{event_id}/crops/{hash}/{detection_id}.jpg`
- Files use SHA256 hashes as object keys for deduplication

## Security Model

- OTP codes hashed with SHA256(secret_key + email + code) before storage
- Download tokens signed with `itsdangerous` (24hr TTL)
- OTP: 10min TTL, max 8 attempts, 60s resend cooldown
- Email normalized (lowercase, stripped) and regex-validated
- Bib numbers normalized to digits only

## Testing

No automated test suite exists. No pytest, unittest, or CI/CD pipeline. Validation is manual via local execution and Docker Compose.

## Dependencies

- `requirements.txt` — full pinned dependency list (86 packages, includes ML stack)
- `requirements.docker.txt` — minimal subset (14 packages, web app only)

## Important Notes

- Model weights (`models/best.pt`, `models/last.ckpt`) are committed to the repo
- The `.dockerignore` excludes most large files but allows `models/best.pt`
- `bib_data.yaml` defines the YOLO dataset config (single class: `bib`)
- The PARSeq pipeline uses PyTorch Lightning checkpoints and Hydra config
- `app.db` (SQLite) exists as a legacy artifact; PostgreSQL is the active database
