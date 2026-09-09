"""
AstroHub Contact profile photos -- Stage 1 (2026-09-09): canonical photo
storage/serving foundation + manual upload/replace. See the approved
architecture investigation this stage implements for the full reasoning;
this module is the ONE place that reasoning is actually enforced in code.

Deliberately separate from luma_contact_enrichment.py -- a different
domain (a single opaque object key + provenance, not multiple text
fields with a recency algorithm) deserves its own module, not a flag
bolted onto that one. It DOES reuse that module's `FIELD_PROVENANCE_KEY`
constant directly (both live at `custom_fields["field_provenance"]`,
just under different sub-keys -- "company"/"title" there, "profile_photo"
here) rather than defining a second, differently-named constant for the
exact same dict.

Storage is accessed ONLY through `ObjectStorageClient` (see
app/storage/object_storage_client.py) -- this module never imports
boto3 or any provider SDK directly, so Cloudflare R2 can be swapped for
any other S3-compatible provider later without touching this file.

Overwrite precedence (centralized in `may_replace_photo` below -- EVERY
photo source, manual or automated, present or future, must call
`apply_candidate_photo`, which is the only path that ever writes
`profile_photo_key`, so this rule can never be silently bypassed by a
future acquisition integration):

  1. A manual upload/replace ALWAYS wins, unconditionally -- a human
     acting deliberately in the CRM UI is never blocked by provenance
     logic, regardless of what's currently stored.
  2. If the CURRENTLY stored photo's provenance source is "manual", NO
     automated source may ever replace it -- only another manual upload
     can (already covered by rule 1). This is a hard gate, not just "the
     highest number in a priority ladder" -- protects a human correction
     from ever being silently overwritten by automation.
  3. Among automated sources, priority is STRICT and per-source-distinct
     (AUTOMATED_SOURCE_PRIORITY below) -- a strictly higher-priority
     source may replace a lower-priority one. A lower-priority candidate
     never replaces a higher-priority stored value.
  4. A candidate from the SAME automated source as what's currently
     stored may replace it only if the candidate's evidence is STRICTLY
     newer (a same-source freshness refresh -- e.g. a fresh Apollo sync
     legitimately replacing an older Apollo-derived photo). Two
     DIFFERENT sources are never allowed to tie at the same priority --
     see AUTOMATED_SOURCE_PRIORITY's own docstring for why that
     ambiguity is avoided by construction rather than resolved after the
     fact.
  5. No existing photo at all -- any candidate may populate it.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger
from PIL import Image, UnidentifiedImageError

from app.services.crm_service import CrmContactNotFound, CrmService
from app.services.luma_contact_enrichment import FIELD_PROVENANCE_KEY
from app.storage.object_storage_client import ObjectStorageClient

MANUAL_SOURCE = "manual"

# Strict, per-source-distinct ranking for AUTOMATED sources only -- "manual"
# is handled entirely separately (rule 2 above), never compared numerically
# against this table. When a new automated provider is added later, give
# it its own distinct rank rather than reusing an existing one -- this
# avoids the "two different sources tied at the same priority" ambiguity
# structurally, instead of trying to resolve it with a recency/confidence
# guess after the fact (see may_replace_photo's rule 4: recency is only
# ever used to arbitrate the SAME source against itself, never two
# different sources that happen to share a rank).
AUTOMATED_SOURCE_PRIORITY: dict[str, int] = {
    "linkedin": 50,
    "apollo": 40,
    "csv_import": 30,
}

ALLOWED_INPUT_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB raw-upload cap -- the OUTPUT is always tiny regardless of input size
CANONICAL_SIZE = 512  # both dimensions -- Sheets' IMAGE() downscales further for its cell display at no cost to us
JPEG_QUALITY = 85
OUTPUT_CONTENT_TYPE = "image/jpeg"  # JPEG only, regardless of input format -- see module docstring on WebP/Sheets risk


class InvalidImageError(Exception):
    """Raised for anything that fails validation OR fails to actually
    decode as a real image -- the declared Content-Type is never trusted
    alone (see validate_and_process_image)."""


# --- image processing (pure -- no I/O, fully unit-testable in isolation) ---


def validate_and_process_image(raw_bytes: bytes, content_type: str | None) -> bytes:
    """raw upload bytes -> normalized JPEG bytes (CANONICAL_SIZE square,
    center-cropped, metadata-stripped). Raises InvalidImageError for
    anything that fails validation or fails to decode -- never returns a
    partially-processed result."""
    if not raw_bytes:
        raise InvalidImageError("Upload is empty.")
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise InvalidImageError(f"Upload exceeds the {MAX_UPLOAD_BYTES} byte limit.")
    if content_type not in ALLOWED_INPUT_CONTENT_TYPES:
        raise InvalidImageError(f"Unsupported content type: {content_type!r}. Allowed: JPEG, PNG, WebP.")

    try:
        image = Image.open(io.BytesIO(raw_bytes))
        image.load()  # force full decode NOW -- catches truncated/corrupt/mislabeled data immediately,
        # rather than trusting a lazily-decoded header that never gets fully validated.
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise InvalidImageError(f"Could not decode upload as an image: {e}") from None

    # Fresh conversion (never re-using the source image's own mode/palette)
    # -- also the first step that drops any EXIF/ICC metadata baked into
    # the original, since the resulting object carries none of it forward.
    rgb = image.convert("RGB")
    cropped = _center_crop_to_square(rgb)
    resized = cropped.resize((CANONICAL_SIZE, CANONICAL_SIZE), Image.LANCZOS)

    output = io.BytesIO()
    # No `exif=` kwarg passed -- confirms metadata stripping (verified: a
    # freshly resized/cropped/converted Image object saved this way carries
    # zero EXIF, regardless of what the original had).
    resized.save(output, format="JPEG", quality=JPEG_QUALITY)
    return output.getvalue()


def _center_crop_to_square(image: Image.Image) -> Image.Image:
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return image.crop((left, top, left + side, top + side))


# --- overwrite precedence (pure -- the ONE place this decision is made) ----


@dataclass(frozen=True)
class PhotoProvenance:
    source: str
    source_url: str | None
    updated_at: datetime


def _parse_provenance(raw: dict | None) -> PhotoProvenance | None:
    if not raw:
        return None
    updated_at_raw = raw.get("updated_at")
    try:
        updated_at = datetime.fromisoformat(updated_at_raw) if updated_at_raw else datetime.min.replace(tzinfo=timezone.utc)
    except ValueError:
        updated_at = datetime.min.replace(tzinfo=timezone.utc)
    return PhotoProvenance(source=raw.get("source", ""), source_url=raw.get("source_url"), updated_at=updated_at)


def may_replace_photo(current_provenance_raw: dict | None, candidate_source: str, candidate_updated_at: datetime) -> bool:
    """The centralized overwrite-precedence decision -- see this module's
    own docstring for the full rule set. `current_provenance_raw` is the
    raw dict currently stored at
    custom_fields["field_provenance"]["profile_photo"] (or None if no
    photo has ever been set)."""
    if candidate_source == MANUAL_SOURCE:
        return True  # rule 1 -- unconditional

    current = _parse_provenance(current_provenance_raw)
    if current is None:
        return True  # rule 5 -- nothing stored yet

    if current.source == MANUAL_SOURCE:
        return False  # rule 2 -- hard gate, no automated source may pass this

    current_priority = AUTOMATED_SOURCE_PRIORITY.get(current.source, -1)
    candidate_priority = AUTOMATED_SOURCE_PRIORITY.get(candidate_source, -1)

    if candidate_priority > current_priority:
        return True  # rule 3

    if candidate_priority == current_priority and candidate_source == current.source:
        return candidate_updated_at > current.updated_at  # rule 4 -- same-source freshness only

    return False


# --- orchestration (the only code path that touches storage + the Contact) -


class ProfilePhotoService:
    def __init__(self, crm_service: CrmService, storage_client: ObjectStorageClient):
        self.crm_service = crm_service
        self.storage_client = storage_client

    async def upload_manual_photo(self, crm_contact_id: str, raw_bytes: bytes, content_type: str | None):
        """The one entry point the manual-upload API route calls. Image
        processing happens BEFORE any I/O (storage or Contact) -- an
        invalid upload never touches either."""
        processed = validate_and_process_image(raw_bytes, content_type)
        result = await self.apply_candidate_photo(
            crm_contact_id, source=MANUAL_SOURCE, source_url=None, processed_jpeg_bytes=processed
        )
        assert result is not None  # manual always passes may_replace_photo -- never a no-op
        return result

    async def apply_candidate_photo(
        self,
        crm_contact_id: str,
        *,
        source: str,
        source_url: str | None,
        processed_jpeg_bytes: bytes,
        candidate_updated_at: datetime | None = None,
    ):
        """The ONE path every photo source -- manual upload today,
        LinkedIn/Apollo/CSV-import enrichment later -- must go through.
        `processed_jpeg_bytes` must already be fully validated/normalized
        JPEG bytes; this method itself does no image processing, only the
        precedence check + the storage/Contact replacement transaction.

        Returns the updated CrmContact, or None if `may_replace_photo`
        rejected the candidate (nothing was touched in that case -- no
        storage call, no Contact write).

        Replacement transaction order (see module's own IMPORTANT
        instruction this stage was built against):
          1. (caller) validate/process the new image -- already done by
             the time this method is called.
          2. Upload the new object. If this fails, the exception
             propagates and NOTHING else has happened -- the existing
             Contact/photo is untouched.
          3. Update the Contact + provenance. If THIS fails, best-effort
             delete the just-uploaded orphan (logged, never masks the
             real error) and re-raise -- the existing Contact/photo
             remains exactly as it was.
          4. Only after the Contact update succeeds, delete the OLD
             object. A failure here is logged and NEVER rolls back the
             already-successful Contact update.
        """
        candidate_updated_at = candidate_updated_at or datetime.now(timezone.utc)

        contact = await self.crm_service.contact_store.get(crm_contact_id)
        if contact is None:
            raise CrmContactNotFound(crm_contact_id)

        current_provenance_raw = (contact.custom_fields.get(FIELD_PROVENANCE_KEY) or {}).get("profile_photo")
        if not may_replace_photo(current_provenance_raw, source, candidate_updated_at):
            return None

        old_key = contact.profile_photo_key
        new_key = f"avatars/{uuid.uuid4()}.jpg"

        await self.storage_client.put_object(new_key, processed_jpeg_bytes, content_type=OUTPUT_CONTENT_TYPE)

        provenance = dict(contact.custom_fields.get(FIELD_PROVENANCE_KEY) or {})
        provenance["profile_photo"] = {
            "source": source,
            "source_url": source_url,
            "updated_at": candidate_updated_at.isoformat(),
        }
        updated_contact = contact.model_copy(
            update={
                "profile_photo_key": new_key,
                "custom_fields": {**contact.custom_fields, FIELD_PROVENANCE_KEY: provenance},
                "updated_at": datetime.now(timezone.utc),
            }
        )

        try:
            await self.crm_service.contact_store.save(updated_contact)
        except Exception:
            try:
                await self.storage_client.delete_object(new_key)
            except Exception as cleanup_error:
                logger.error(
                    f"Profile photo: failed to clean up orphaned object {new_key!r} after a Contact "
                    f"save error for {crm_contact_id} -- {type(cleanup_error).__name__} (no credentials logged)."
                )
            raise

        if old_key:
            try:
                await self.storage_client.delete_object(old_key)
            except Exception as delete_error:
                # Never roll back -- the Contact update already succeeded
                # and is the source of truth; this only leaves one
                # orphaned old object behind for later cleanup.
                logger.error(
                    f"Profile photo: failed to delete old object {old_key!r} for {crm_contact_id} after "
                    f"a successful replacement -- {type(delete_error).__name__} (no credentials logged)."
                )

        return updated_contact
