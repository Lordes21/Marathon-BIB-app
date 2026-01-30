import os
import io
import re
import time
import zipfile
import secrets
import hashlib
import smtplib
from email.message import EmailMessage

import boto3
import psycopg2
from psycopg2.extras import RealDictCursor

from flask import Flask, render_template, request, send_file
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# =========================
# Configuration (Production: Postgres + MinIO)
# =========================
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
serializer = URLSafeTimedSerializer(app.secret_key)

# Event to serve (single-event mode for now)
EVENT_ID = int(os.environ.get("EVENT_ID", "1"))
PG_SCHEMA = os.environ.get("PG_SCHEMA", "marathon")

# Postgres
PG_HOST = os.environ.get("PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("PG_PORT", "5433"))
PG_DB = os.environ.get("PG_DB", "marathon_db")
PG_USER = os.environ.get("PG_USER", "marathon")
PG_PASS = os.environ.get("PG_PASS", "123456")

# MinIO / S3
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://localhost:9000")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "minioadmin123")
S3_BUCKET = os.environ.get("S3_BUCKET", "marathon")

# OTP + download link settings
OTP_TTL_SECONDS = 10 * 60                 # 10 minutes
OTP_MIN_RESEND_SECONDS = 60               # resend once per minute
OTP_MAX_ATTEMPTS = 8                      # attempts per OTP
DOWNLOAD_LINK_TTL_SECONDS = 24 * 60 * 60  # 24 hours

# Email (SMTP) settings
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
SMTP_FROM = os.environ.get("SMTP_FROM")  # e.g. "Berat Marathon <yourgmail@gmail.com>"


# =========================
# Helpers
# =========================
def normalize_email(x: str) -> str:
    return (x or "").strip().lower()


def normalize_bib(x: str) -> str:
    return "".join(ch for ch in (x or "").strip() if ch.isdigit())


def is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


def smtp_configured() -> bool:
    return all([SMTP_HOST, SMTP_USER, SMTP_PASS, SMTP_FROM])


def send_email(to_email: str, subject: str, text: str) -> None:
    if not smtp_configured():
        raise RuntimeError("SMTP not configured (set SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/SMTP_FROM).")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg.set_content(text)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


def pg():
    conn = psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        dbname=PG_DB,
        user=PG_USER,
        password=PG_PASS,
        cursor_factory=RealDictCursor,
    )
    return conn


def get_s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name="us-east-1",
    )


def init_db():
    # OTP table for production (in same schema)
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA};")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.otp_requests (
                    email TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL,
                    expires_at BIGINT NOT NULL,
                    last_sent_at BIGINT NOT NULL,
                    attempts_left INTEGER NOT NULL
                )
            """)
        conn.commit()


def hash_code(email: str, code: str) -> str:
    salt = (app.secret_key + "|" + email).encode("utf-8")
    return hashlib.sha256(salt + code.encode("utf-8")).hexdigest()


def lookup_bib_for_email(email: str, event_id: int | None = None) -> str:
    """
    Postgres source of truth:
      marathon.registrations(event_id, email, bib)
    """
    email = normalize_email(email)
    if not email:
        return ""
    eid = event_id if event_id else EVENT_ID

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT bib
                FROM {PG_SCHEMA}.registrations
                WHERE event_id = %s AND lower(email) = %s
                LIMIT 1
                """,
                (eid, email),
            )
            row = cur.fetchone()

    return normalize_bib(row["bib"]) if row else ""


def get_photo_keys_for_bib(bib_number: str, event_id: int | None = None):
    """
    Postgres source of truth:
      marathon.matches(event_id, bib, photo_id)
      marathon.photos(id, file_path) where file_path is MinIO object key for ORIGINAL image
    Returns list of MinIO object keys.
    """
    bib_number = normalize_bib(bib_number)
    if not bib_number:
        return []
    eid = event_id if event_id else EVENT_ID

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT p.file_path
                FROM {PG_SCHEMA}.matches m
                JOIN {PG_SCHEMA}.photos p ON p.id = m.photo_id
                WHERE m.event_id = %s AND m.bib = %s
                GROUP BY p.file_path
                ORDER BY MIN(p.id)
                """,
                (eid, bib_number),
            )
            rows = cur.fetchall()

    return [r["file_path"] for r in rows]


def build_zip_bytes_from_s3(keys, zip_basename: str):
    mem = io.BytesIO()
    s3 = get_s3()

    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        used = set()
        for key in keys:
            obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
            data = obj["Body"].read()

            arcname = os.path.basename(key) or "photo.jpg"
            # ensure unique names in zip
            base, ext = os.path.splitext(arcname)
            n = 1
            name = arcname
            while name in used:
                name = f"{base}_{n}{ext}"
                n += 1
            used.add(name)

            z.writestr(name, data)

    mem.seek(0)
    return mem, f"{zip_basename}.zip"


def make_download_token(email: str, bib: str, event_id: int | None = None) -> str:
    eid = event_id if event_id else EVENT_ID
    payload = {"event_id": eid, "email": normalize_email(email), "bib": normalize_bib(bib)}
    return serializer.dumps(payload)


def read_download_token(token: str):
    try:
        payload = serializer.loads(token, max_age=DOWNLOAD_LINK_TTL_SECONDS)
        return payload
    except SignatureExpired:
        return {"error": "expired"}
    except BadSignature:
        return {"error": "bad"}


def log_email(event_id: int, email: str, bib: str, photo_count: int, status: str, error: str | None = None):
    try:
        with pg() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {PG_SCHEMA}.email_log (event_id, email, bib, photo_count, status, error)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (event_id, email, bib, int(photo_count), status, error),
                )
            conn.commit()
    except Exception:
        # Don't break user flow if logging fails
        pass


# =========================
# Helpers – events
# =========================
def get_events():
    """Fetch all events from the database."""
    try:
        with pg() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id, name, event_date FROM {PG_SCHEMA}.events ORDER BY event_date DESC, id DESC"
                )
                return cur.fetchall()
    except Exception:
        return []


def resolve_event_id(form_event_id):
    """Return a valid integer event_id from the form, falling back to EVENT_ID."""
    try:
        eid = int(form_event_id)
        if eid > 0:
            return eid
    except (TypeError, ValueError):
        pass
    return EVENT_ID


# =========================
# Routes
# =========================
@app.route("/", methods=["GET"])
def index():
    events = get_events()
    return render_template("index.html", smtp_ok=smtp_configured(), events=events)


@app.route("/request_code", methods=["POST"])
def request_code():
    init_db()

    email = normalize_email(request.form.get("email", ""))
    event_id = resolve_event_id(request.form.get("event_id"))
    events = get_events()

    if not is_valid_email(email):
        return render_template(
            "index.html",
            smtp_ok=smtp_configured(),
            events=events,
            selected_event=event_id,
            email_value=email,
            error="Please enter a valid email.",
        )

    now = int(time.time())

    # Rate limit resend
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {PG_SCHEMA}.otp_requests WHERE email = %s", (email,))
            row = cur.fetchone()

    if row and (now - int(row["last_sent_at"])) < OTP_MIN_RESEND_SECONDS:
        return render_template(
            "verify.html",
            email=email,
            event_id=event_id,
            smtp_ok=smtp_configured(),
            info="A code was recently sent. Please wait a moment and try again.",
        )

    # Generate OTP (6 digits)
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_h = hash_code(email, code)
    expires_at = now + OTP_TTL_SECONDS

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {PG_SCHEMA}.otp_requests(email, code_hash, expires_at, last_sent_at, attempts_left)
                VALUES(%s, %s, %s, %s, %s)
                ON CONFLICT(email) DO UPDATE SET
                  code_hash = EXCLUDED.code_hash,
                  expires_at = EXCLUDED.expires_at,
                  last_sent_at = EXCLUDED.last_sent_at,
                  attempts_left = EXCLUDED.attempts_left
                """,
                (email, code_h, expires_at, now, OTP_MAX_ATTEMPTS),
            )
        conn.commit()

    if not smtp_configured():
        return render_template(
            "verify.html",
            email=email,
            event_id=event_id,
            smtp_ok=False,
            error="Email sending is not configured on the server. Configure SMTP first.",
        )

    try:
        send_email(
            to_email=email,
            subject="Your Run Lens verification code",
            text=(
                f"Your verification code is: {code}\n\n"
                f"It expires in {OTP_TTL_SECONDS // 60} minutes.\n"
                f"If you did not request this, you can ignore this email."
            ),
        )
    except Exception as e:
        return render_template(
            "verify.html", email=email, event_id=event_id, smtp_ok=smtp_configured(), error=f"Email error: {e}"
        )

    return render_template(
        "verify.html",
        email=email,
        event_id=event_id,
        smtp_ok=smtp_configured(),
        info="We sent a verification code to your email. Enter it below.",
    )


@app.route("/verify_and_send", methods=["POST"])
def verify_and_send():
    init_db()

    email = normalize_email(request.form.get("email", ""))
    code = (request.form.get("code", "") or "").strip()
    event_id = resolve_event_id(request.form.get("event_id"))

    if not is_valid_email(email) or not re.match(r"^\d{6}$", code):
        return render_template(
            "verify.html", email=email, event_id=event_id, smtp_ok=smtp_configured(), error="Invalid code format."
        )

    now = int(time.time())

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {PG_SCHEMA}.otp_requests WHERE email = %s", (email,))
            row = cur.fetchone()

            if not row:
                return render_template(
                    "verify.html", email=email, event_id=event_id, smtp_ok=smtp_configured(),
                    error="Please request a new code.",
                )

            if now > int(row["expires_at"]):
                cur.execute(f"DELETE FROM {PG_SCHEMA}.otp_requests WHERE email = %s", (email,))
                conn.commit()
                return render_template(
                    "verify.html", email=email, event_id=event_id, smtp_ok=smtp_configured(),
                    error="Code expired. Request a new one.",
                )

            attempts_left = int(row["attempts_left"])
            if attempts_left <= 0:
                cur.execute(f"DELETE FROM {PG_SCHEMA}.otp_requests WHERE email = %s", (email,))
                conn.commit()
                return render_template(
                    "verify.html", email=email, event_id=event_id, smtp_ok=smtp_configured(),
                    error="Too many attempts. Request a new code.",
                )

            expected = row["code_hash"]
            if hash_code(email, code) != expected:
                attempts_left -= 1
                cur.execute(
                    f"UPDATE {PG_SCHEMA}.otp_requests SET attempts_left = %s WHERE email = %s",
                    (attempts_left, email),
                )
                conn.commit()
                return render_template(
                    "verify.html", email=email, event_id=event_id, smtp_ok=smtp_configured(),
                    error=f"Incorrect code. Attempts left: {attempts_left}.",
                )

            # Verified: consume OTP
            cur.execute(f"DELETE FROM {PG_SCHEMA}.otp_requests WHERE email = %s", (email,))
        conn.commit()

    # Now deliver using DB (use selected event_id)
    bib = lookup_bib_for_email(email, event_id)
    if not bib:
        return render_template(
            "done.html",
            smtp_ok=smtp_configured(),
            message="Verification successful. If photos are available for your registration, you will receive an email shortly.",
        )

    keys = get_photo_keys_for_bib(bib, event_id)
    if not keys:
        return render_template(
            "done.html",
            smtp_ok=smtp_configured(),
            message="Verification successful. We did not find any photos yet. Please check back later.",
        )

    # Signed download link
    token = make_download_token(email=email, bib=bib, event_id=event_id)
    download_url = request.url_root.rstrip("/") + f"/download/{token}"

    try:
        send_email(
            to_email=email,
            subject="Your race photos are ready",
            text=(
                "Your photos are ready.\n\n"
                f"Download your ZIP here (link expires in 24 hours):\n{download_url}\n\n"
                "If you did not request this email, ignore it."
            ),
        )
        log_email(event_id, email, bib, photo_count=len(keys), status="sent", error=None)
    except Exception as e:
        log_email(event_id, email, bib, photo_count=len(keys), status="failed", error=str(e))
        return render_template("done.html", smtp_ok=smtp_configured(), message=f"Email error: {e}")

    return render_template("done.html", smtp_ok=smtp_configured(), message="Success. We sent your download link to your email.")


@app.route("/download/<token>", methods=["GET"])
def download(token: str):
    payload = read_download_token(token)
    if payload.get("error") == "expired":
        return "This download link has expired. Please request a new one.", 410
    if payload.get("error") == "bad":
        return "Invalid download link.", 403

    event_id = int(payload.get("event_id", 0))
    if event_id <= 0:
        return "Invalid link.", 403

    email = normalize_email(payload.get("email", ""))
    bib = normalize_bib(payload.get("bib", ""))
    if not email or not bib:
        return "Invalid link.", 403

    # Ensure mapping still matches (DB)
    if lookup_bib_for_email(email, event_id) != bib:
        return "This link is no longer valid.", 403

    keys = get_photo_keys_for_bib(bib, event_id)
    if not keys:
        return "No photos found.", 404

    mem, zip_name = build_zip_bytes_from_s3(keys, zip_basename=f"bib_{bib}_photos")
    return send_file(mem, mimetype="application/zip", as_attachment=True, download_name=zip_name)


if __name__ == "__main__":
    init_db()
    print("[INFO] Starting Run Lens app")
    print(f"[INFO] Default EVENT_ID: {EVENT_ID}")
    print(f"[INFO] Postgres: {PG_HOST}:{PG_PORT}/{PG_DB} schema={PG_SCHEMA}")
    print(f"[INFO] MinIO/S3: {S3_ENDPOINT} bucket={S3_BUCKET}")
    print(f"[INFO] SMTP configured: {smtp_configured()}")
    app.run(debug=True)
