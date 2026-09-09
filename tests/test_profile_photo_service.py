"""
Tests for app/services/profile_photo_service.py -- Stage 1 (canonical
Contact photo foundation + manual upload/replace). Storage is always the
in-memory MemoryObjectStorageClient here -- no real Cloudflare R2 call is
ever made by this test file.
"""

import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from PIL import Image

from app.models.crm import CrmContact
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.services.crm_service import CrmService
from app.services.luma_contact_enrichment import FIELD_PROVENANCE_KEY
from app.services.profile_photo_service import (
    AUTOMATED_SOURCE_PRIORITY,
    CANONICAL_SIZE,
    MANUAL_SOURCE,
    MAX_UPLOAD_BYTES,
    InvalidImageError,
    ProfilePhotoService,
    may_replace_photo,
    validate_and_process_image,
)
from app.storage.memory_object_storage_client import MemoryObjectStorageClient

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_contact(**overrides) -> CrmContact:
    defaults = dict(crm_contact_id=str(uuid.uuid4()), created_at=_now(), updated_at=_now())
    defaults.update(overrides)
    return CrmContact(**defaults)


def _make_image_bytes(size=(800, 600), color=(255, 0, 0), fmt="JPEG", with_exif=False) -> bytes:
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    if with_exif:
        exif = img.getexif()
        exif[0x9003] = "2020:01:01 00:00:00"  # DateTimeOriginal
        exif[0x0112] = 1  # Orientation
        img.save(buf, format=fmt, exif=exif.tobytes())
    else:
        img.save(buf, format=fmt)
    return buf.getvalue()


# =============================================================================
# Image processing (pure functions -- no I/O)
# =============================================================================


def test_valid_jpeg_upload_is_processed_to_canonical_square_jpeg():
    processed = validate_and_process_image(_make_image_bytes(fmt="JPEG"), "image/jpeg")
    img = Image.open(io.BytesIO(processed))
    assert img.size == (CANONICAL_SIZE, CANONICAL_SIZE)
    assert img.format == "JPEG"


def test_valid_png_upload_is_processed_to_canonical_square_jpeg():
    processed = validate_and_process_image(_make_image_bytes(size=(300, 500), fmt="PNG"), "image/png")
    img = Image.open(io.BytesIO(processed))
    assert img.size == (CANONICAL_SIZE, CANONICAL_SIZE)
    assert img.format == "JPEG"


def test_valid_webp_upload_is_processed_to_canonical_square_jpeg():
    processed = validate_and_process_image(_make_image_bytes(size=(400, 400), fmt="WEBP"), "image/webp")
    img = Image.open(io.BytesIO(processed))
    assert img.size == (CANONICAL_SIZE, CANONICAL_SIZE)
    assert img.format == "JPEG"


def test_metadata_is_stripped_from_the_processed_output():
    raw = _make_image_bytes(with_exif=True)
    assert dict(Image.open(io.BytesIO(raw)).getexif())  # sanity: the input really does carry EXIF
    processed = validate_and_process_image(raw, "image/jpeg")
    assert dict(Image.open(io.BytesIO(processed)).getexif()) == {}


def test_non_square_input_is_center_cropped_to_canonical_size():
    processed = validate_and_process_image(_make_image_bytes(size=(1000, 200)), "image/jpeg")
    img = Image.open(io.BytesIO(processed))
    assert img.size == (CANONICAL_SIZE, CANONICAL_SIZE)


def test_spoofed_content_type_is_rejected_because_it_fails_to_decode():
    """Plain text bytes claiming to be a JPEG -- decodability is what's
    actually checked, never the declared Content-Type alone."""
    with pytest.raises(InvalidImageError):
        validate_and_process_image(b"this is not an image, just plain text" * 50, "image/jpeg")


def test_unsupported_content_type_is_rejected():
    raw = _make_image_bytes()
    with pytest.raises(InvalidImageError):
        validate_and_process_image(raw, "image/gif")


def test_oversized_upload_is_rejected_before_decoding():
    with pytest.raises(InvalidImageError):
        validate_and_process_image(b"x" * (MAX_UPLOAD_BYTES + 1), "image/jpeg")


def test_empty_upload_is_rejected():
    with pytest.raises(InvalidImageError):
        validate_and_process_image(b"", "image/jpeg")


# =============================================================================
# Overwrite precedence (pure -- may_replace_photo)
# =============================================================================


def test_manual_always_wins_when_nothing_is_stored():
    assert may_replace_photo(None, MANUAL_SOURCE, _now()) is True


def test_manual_always_wins_over_an_existing_automated_photo():
    current = {"source": "linkedin", "source_url": None, "updated_at": _now().isoformat()}
    assert may_replace_photo(current, MANUAL_SOURCE, _now()) is True


def test_manual_is_a_hard_gate_no_automated_source_can_pass_it():
    current = {"source": MANUAL_SOURCE, "source_url": None, "updated_at": _now().isoformat()}
    far_future = _now() + timedelta(days=3650)
    for automated_source in AUTOMATED_SOURCE_PRIORITY:
        assert may_replace_photo(current, automated_source, far_future) is False


def test_higher_priority_automated_source_replaces_lower_priority():
    current = {"source": "csv_import", "source_url": None, "updated_at": _now().isoformat()}
    assert may_replace_photo(current, "apollo", _now()) is True
    assert may_replace_photo(current, "linkedin", _now()) is True


def test_lower_priority_automated_source_never_replaces_higher_priority():
    far_future = _now() + timedelta(days=3650)
    current = {"source": "linkedin", "source_url": None, "updated_at": _now().isoformat()}
    assert may_replace_photo(current, "apollo", far_future) is False
    assert may_replace_photo(current, "csv_import", far_future) is False


def test_same_source_strictly_newer_evidence_replaces_older():
    now = _now()
    current = {"source": "apollo", "source_url": None, "updated_at": now.isoformat()}
    assert may_replace_photo(current, "apollo", now + timedelta(days=1)) is True


def test_same_source_equal_or_older_evidence_does_not_replace():
    now = _now()
    current = {"source": "apollo", "source_url": None, "updated_at": now.isoformat()}
    assert may_replace_photo(current, "apollo", now) is False  # not strictly newer
    assert may_replace_photo(current, "apollo", now - timedelta(days=1)) is False


def test_any_candidate_populates_when_nothing_is_stored():
    for source in [*AUTOMATED_SOURCE_PRIORITY, MANUAL_SOURCE]:
        assert may_replace_photo(None, source, _now()) is True


# =============================================================================
# Orchestration -- ProfilePhotoService (storage + Contact transaction)
# =============================================================================


@pytest_asyncio.fixture
async def crm_service():
    return CrmService(contact_store=MemoryCrmContactStore())


@pytest.fixture
def storage_client():
    return MemoryObjectStorageClient()


@pytest.fixture
def photo_service(crm_service, storage_client):
    return ProfilePhotoService(crm_service=crm_service, storage_client=storage_client)


async def test_upload_manual_photo_creates_object_and_updates_contact(crm_service, storage_client, photo_service):
    contact = make_contact()
    await crm_service.contact_store.create(contact)

    updated = await photo_service.upload_manual_photo(contact.crm_contact_id, _make_image_bytes(), "image/jpeg")

    assert updated.profile_photo_key is not None
    assert updated.profile_photo_key in storage_client.objects
    provenance = updated.custom_fields[FIELD_PROVENANCE_KEY]["profile_photo"]
    assert provenance["source"] == MANUAL_SOURCE
    assert provenance["source_url"] is None
    assert provenance["updated_at"] is not None


async def test_object_key_contains_no_contact_id_email_or_name(crm_service, storage_client, photo_service):
    contact = make_contact(email="alice@example.com", first_name="Alice", last_name="Angel")
    await crm_service.contact_store.create(contact)

    updated = await photo_service.upload_manual_photo(contact.crm_contact_id, _make_image_bytes(), "image/jpeg")

    key = updated.profile_photo_key
    assert contact.crm_contact_id not in key
    assert "alice" not in key.lower()
    assert "angel" not in key.lower()
    assert "example.com" not in key.lower()


async def test_no_photo_yields_null_url(crm_service):
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    assert contact.profile_photo_url is None


async def test_photo_present_yields_derived_url(monkeypatch, crm_service, photo_service):
    from app.config import settings

    monkeypatch.setattr(settings, "profile_photo_cdn_base_url", "https://cdn.example.com")
    contact = make_contact()
    await crm_service.contact_store.create(contact)

    updated = await photo_service.upload_manual_photo(contact.crm_contact_id, _make_image_bytes(), "image/jpeg")

    assert updated.profile_photo_url == f"https://cdn.example.com/{updated.profile_photo_key}"


async def test_replacement_uploads_new_object_before_deleting_old(crm_service, storage_client, photo_service):
    contact = make_contact()
    await crm_service.contact_store.create(contact)

    first = await photo_service.upload_manual_photo(contact.crm_contact_id, _make_image_bytes(color=(255, 0, 0)), "image/jpeg")
    old_key = first.profile_photo_key
    assert old_key in storage_client.objects

    second = await photo_service.upload_manual_photo(contact.crm_contact_id, _make_image_bytes(color=(0, 0, 255)), "image/jpeg")
    new_key = second.profile_photo_key

    assert new_key != old_key
    assert new_key in storage_client.objects
    assert old_key not in storage_client.objects  # deleted only AFTER the successful replace
    assert storage_client.deleted_keys == [old_key]


async def test_failed_new_object_upload_leaves_existing_photo_completely_untouched(crm_service, storage_client, photo_service):
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    first = await photo_service.upload_manual_photo(contact.crm_contact_id, _make_image_bytes(), "image/jpeg")
    old_key = first.profile_photo_key

    storage_client.fail_put = True
    with pytest.raises(ConnectionError):
        await photo_service.upload_manual_photo(contact.crm_contact_id, _make_image_bytes(color=(0, 0, 255)), "image/jpeg")

    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert persisted.profile_photo_key == old_key
    assert old_key in storage_client.objects  # never touched
    assert storage_client.deleted_keys == []


async def test_failed_contact_persistence_cleans_up_orphan_and_leaves_old_photo_intact(crm_service, storage_client):
    class ConditionallyFailingContactStore(MemoryCrmContactStore):
        def __init__(self):
            super().__init__()
            self.fail_next_save = False

        async def save(self, contact):
            if self.fail_next_save:
                raise RuntimeError("simulated Contact persistence failure")
            await super().save(contact)

    failing_store = ConditionallyFailingContactStore()
    crm_service_with_failing_store = CrmService(contact_store=failing_store)
    service = ProfilePhotoService(crm_service=crm_service_with_failing_store, storage_client=storage_client)

    contact = make_contact()
    await failing_store.create(contact)
    first = await service.upload_manual_photo(contact.crm_contact_id, _make_image_bytes(), "image/jpeg")
    old_key = first.profile_photo_key

    failing_store.fail_next_save = True
    with pytest.raises(RuntimeError):
        await service.upload_manual_photo(contact.crm_contact_id, _make_image_bytes(color=(0, 0, 255)), "image/jpeg")

    persisted = await failing_store.get(contact.crm_contact_id)
    assert persisted.profile_photo_key == old_key  # Contact untouched
    assert old_key in storage_client.objects  # old photo intact
    # the orphaned NEW object (uploaded before the failed save) was cleaned up --
    # only the old key remains in storage.
    assert set(storage_client.objects.keys()) == {old_key}


async def test_failed_old_object_deletion_does_not_roll_back_a_successful_replacement(crm_service, storage_client, photo_service):
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    first = await photo_service.upload_manual_photo(contact.crm_contact_id, _make_image_bytes(), "image/jpeg")
    old_key = first.profile_photo_key

    storage_client.fail_delete_keys = {old_key}
    second = await photo_service.upload_manual_photo(contact.crm_contact_id, _make_image_bytes(color=(0, 0, 255)), "image/jpeg")

    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert persisted.profile_photo_key == second.profile_photo_key
    assert persisted.profile_photo_key != old_key
    assert old_key in storage_client.objects  # orphaned, but the replacement itself was NOT rolled back


async def test_apply_candidate_photo_rejects_lower_priority_source_as_a_pure_noop(crm_service, storage_client, photo_service):
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    linkedin_result = await photo_service.apply_candidate_photo(
        contact.crm_contact_id, source="linkedin", source_url="https://linkedin.example/photo.jpg",
        processed_jpeg_bytes=_make_image_bytes(),
    )
    assert linkedin_result is not None

    csv_result = await photo_service.apply_candidate_photo(
        contact.crm_contact_id, source="csv_import", source_url=None, processed_jpeg_bytes=_make_image_bytes(color=(0, 0, 255)),
    )

    assert csv_result is None
    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert persisted.profile_photo_key == linkedin_result.profile_photo_key
    assert len(storage_client.objects) == 1  # the rejected candidate's bytes were never even uploaded


async def test_apply_candidate_photo_accepts_higher_priority_source(crm_service, photo_service):
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    csv_result = await photo_service.apply_candidate_photo(
        contact.crm_contact_id, source="csv_import", source_url=None, processed_jpeg_bytes=_make_image_bytes(),
    )
    assert csv_result is not None

    linkedin_result = await photo_service.apply_candidate_photo(
        contact.crm_contact_id, source="linkedin", source_url="https://linkedin.example/photo.jpg",
        processed_jpeg_bytes=_make_image_bytes(color=(0, 0, 255)),
    )

    assert linkedin_result is not None
    assert linkedin_result.profile_photo_key != csv_result.profile_photo_key


async def test_apply_candidate_photo_same_source_freshness(crm_service, photo_service):
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    now = _now()
    first = await photo_service.apply_candidate_photo(
        contact.crm_contact_id, source="apollo", source_url=None, processed_jpeg_bytes=_make_image_bytes(),
        candidate_updated_at=now,
    )
    assert first is not None

    stale = await photo_service.apply_candidate_photo(
        contact.crm_contact_id, source="apollo", source_url=None, processed_jpeg_bytes=_make_image_bytes(color=(0, 0, 255)),
        candidate_updated_at=now - timedelta(days=1),
    )
    assert stale is None

    fresh = await photo_service.apply_candidate_photo(
        contact.crm_contact_id, source="apollo", source_url=None, processed_jpeg_bytes=_make_image_bytes(color=(0, 255, 0)),
        candidate_updated_at=now + timedelta(days=1),
    )
    assert fresh is not None
    assert fresh.profile_photo_key != first.profile_photo_key


async def test_manual_upload_permanently_blocks_even_the_highest_priority_automated_source(crm_service, photo_service):
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    await photo_service.apply_candidate_photo(
        contact.crm_contact_id, source="linkedin", source_url=None, processed_jpeg_bytes=_make_image_bytes(),
    )
    manual = await photo_service.upload_manual_photo(contact.crm_contact_id, _make_image_bytes(color=(0, 0, 255)), "image/jpeg")

    blocked = await photo_service.apply_candidate_photo(
        contact.crm_contact_id, source="linkedin", source_url=None, processed_jpeg_bytes=_make_image_bytes(color=(0, 255, 0)),
        candidate_updated_at=_now() + timedelta(days=3650),
    )

    assert blocked is None
    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert persisted.profile_photo_key == manual.profile_photo_key


async def test_archiving_a_contact_does_not_delete_or_clear_its_photo(crm_service, storage_client, photo_service):
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    uploaded = await photo_service.upload_manual_photo(contact.crm_contact_id, _make_image_bytes(), "image/jpeg")
    key = uploaded.profile_photo_key

    archived = await crm_service.archive_contact(contact.crm_contact_id)

    assert archived.archived is True
    assert archived.profile_photo_key == key
    assert key in storage_client.objects  # never deleted
    assert storage_client.deleted_keys == []


# =============================================================================
# Real (unconfigured) R2 client -- production deployment-readiness audit,
# 2026-09-09: proves the actual upload operation fails closed when R2 has
# no configuration, without touching the Contact, using the REAL
# R2ObjectStorageClient rather than the in-memory fake used everywhere else
# in this file.
# =============================================================================


async def test_upload_fails_closed_when_r2_is_unconfigured_and_leaves_contact_untouched(crm_service):
    from app.storage.r2_object_storage_client import R2NotConfiguredError, R2ObjectStorageClient

    unconfigured_client = R2ObjectStorageClient(bucket_name=None, endpoint_url=None, access_key_id=None, secret_access_key=None)
    service = ProfilePhotoService(crm_service=crm_service, storage_client=unconfigured_client)
    contact = make_contact()
    await crm_service.contact_store.create(contact)

    with pytest.raises(R2NotConfiguredError):
        await service.upload_manual_photo(contact.crm_contact_id, _make_image_bytes(), "image/jpeg")

    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert persisted.profile_photo_key is None  # completely untouched
    assert persisted.custom_fields == {}
