from io import BytesIO
from pathlib import Path
import socket
import struct

from PIL import Image, UnidentifiedImageError

from app.core.config import settings
from app.core.errors import require
from app.core.security import new_id
from app.services.records import get, put, rows


def _scan_for_malware(content):
    if not settings.clamav_host:
        require(
            not settings.require_malware_scan,
            "File security scanning is temporarily unavailable",
            503,
        )
        return
    try:
        with socket.create_connection(
            (settings.clamav_host, settings.clamav_port), timeout=8
        ) as scanner:
            scanner.sendall(b"zINSTREAM\0")
            for offset in range(0, len(content), 64 * 1024):
                chunk = content[offset : offset + 64 * 1024]
                scanner.sendall(struct.pack("!I", len(chunk)) + chunk)
            scanner.sendall(struct.pack("!I", 0))
            scanner.settimeout(15)
            verdict = scanner.recv(4096).decode("utf-8", "replace")
    except OSError as exc:
        from app.core.errors import AppError

        if settings.require_malware_scan:
            raise AppError("File security scanning is temporarily unavailable", 503) from exc
        return
    require("FOUND" not in verdict, "This file failed the security scan", 400)
    require(verdict.rstrip("\0\r\n").endswith("OK"), "File security scanning is temporarily unavailable", 503)


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
    require(
        kind in ["AVATAR", "KYC", "POD", "TRUCK_PHOTO", "TRUCK_DOCUMENT", "DRIVER_DOCUMENT", "MESSAGE"],
        "Invalid file kind",
        400,
    )
    _scan_for_malware(content)
    require(
        0 < len(content) <= settings.max_upload_bytes,
        f"This file is too large. The maximum size for one file is {settings.max_upload_bytes // (1024 * 1024)} MB.",
        413,
    )
    used = sum(row.data["size"] for row in rows(db, "uploads") if row.owner_id == party.id)
    require(
        used + len(content) <= settings.max_account_upload_bytes,
        f"Your account has reached its {settings.max_account_upload_bytes // (1024 * 1024)} MB upload limit. Remove files you no longer need or contact support for more storage.",
        413,
    )
    if content.startswith(b"%PDF-"):
        require(
            kind in ["KYC", "TRUCK_DOCUMENT", "DRIVER_DOCUMENT", "MESSAGE"],
            "PDFs are not accepted for this upload",
            400,
        )
        # Reject active PDF features. This is deliberately conservative for
        # operational documents and complements (but does not replace) malware
        # scanning at the deployment boundary.
        lowered = content.lower()
        require(
            not any(
                marker in lowered
                for marker in (
                    b"/javascript",
                    b"/js",
                    b"/launch",
                    b"/embeddedfile",
                    b"/openaction",
                    b"/aa",
                )
            ),
            "This PDF contains active or embedded content and cannot be uploaded",
            400,
        )
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
