# ingest_photos.py
import hashlib
from pathlib import Path
import psycopg2
from storage import get_s3, S3_BUCKET

DB_CONFIG = {
    "host": "db",  # the name that its on docker
    "port": 5433,              # IMPORTANT: your host port
    "dbname": "marathon_db",
    "user": "marathon",
    "password": "123456",
}

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def get_event_photo_folder(cur, event_id: int) -> str:
    """Read the photo_folder for an event. Falls back to 'events/<id>/photos'."""
    cur.execute(
        "SELECT photo_folder FROM marathon.events WHERE id = %s",
        (event_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Event {event_id} not found in the database.")
    folder = (row[0] or "").strip()
    if not folder:
        folder = f"events/{event_id}/photos"
    return folder.rstrip("/")


def ingest_photos(event_id: int, folder: str):
    folder_path = Path(folder)
    files = list(folder_path.rglob("*"))
    print("FOUND FILES:", len(files))

    if not folder_path.exists():
        raise FileNotFoundError(f"Folder not found: {folder_path}")

    s3 = get_s3()
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    # Get the S3 prefix for this event from the DB
    s3_prefix = get_event_photo_folder(cur, event_id)
    print(f"S3 prefix for event {event_id}: {s3_prefix}")

    exts = {".jpg", ".jpeg", ".png"}

    for img in folder_path.rglob("*"):
        if not img.is_file() or img.suffix.lower() not in exts:
            continue

        file_hash = sha256_file(img)
        key = f"{s3_prefix}/{file_hash}{img.suffix.lower()}"

        # Upload (idempotent enough for dev; can optimize later)
        s3.upload_file(str(img), S3_BUCKET, key)

        # Insert row (idempotent)
        cur.execute("""
            INSERT INTO marathon.photos (event_id, file_path, file_hash)
            VALUES (%s, %s, %s)
            ON CONFLICT (event_id, file_path) DO NOTHING
        """, (event_id, key, file_hash))

        print(f"INGESTED: {img.name} -> {key}")

    conn.commit()
    cur.close()
    conn.close()

if __name__ == "__main__":
    # CHANGE THIS:
    ingest_photos(event_id=1, folder=r"data/originals")
