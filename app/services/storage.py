from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.core.config import settings
from app.core.errors import require
from app.core.security import new_id
from app.services.records import get, put, rows


def own_upload(db, party, url, kind):
    prefix = "/api/uploads/"
    require(url.startswith(prefix), "Use a file uploaded to TAMP", 400)
    record = get(db, "uploads", url[len(prefix) :])
    require(
        record is not None and record.owner_id == party.id and record.data["kind"] == kind,
        "Upload is not owned by this account",
        403,
    )
    return record


def store(db, party, content, filename, kind):
    require(kind in ["AVATAR", "KYC", "POD", "TRUCK_PHOTO"], "Invalid file kind", 400)
    require(0 < len(content) <= settings.max_upload_bytes, "File exceeds size limit", 413)
    used = sum(row.data["size"] for row in rows(db, "uploads") if row.owner_id == party.id)
    require(
        used + len(content) <= settings.max_account_upload_bytes,
        "Account upload quota exceeded",
        413,
    )
    if content.startswith(b"%PDF-"):
        require(kind == "KYC", "PDFs are only accepted for KYC", 400)
        mime = "application/pdf"
    else:
        try:
            with Image.open(BytesIO(content)) as im:
                require(im.width * im.height <= 25000000, "Image dimensions exceed limit", 400)
                im.verify()
            with Image.open(BytesIO(content)) as im:
                clean = BytesIO()
                im.convert("RGB").save(clean, format="JPEG", quality=90)
                content = clean.getvalue()
            mime = "image/jpeg"
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            from app.core.errors import AppError

            raise AppError("Upload a valid image or KYC PDF", 400) from exc
    ident = new_id("UP")
    path = Path(settings.upload_path)
    path.mkdir(parents=True, exist_ok=True)
    target = path / ident
    target.write_bytes(content)
    data = dict(
        id=ident,
        url="/api/uploads/" + ident,
        kind=kind,
        filename=Path(filename or "upload").name[:200],
        contentType=mime,
        size=len(content),
    )
    try:
        put(db, "uploads", data, party.id)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return data
