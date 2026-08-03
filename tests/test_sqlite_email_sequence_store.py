"""
Persistence tests for SQLiteEmailSequenceStore and SQLiteEmailSequenceStepStore
-- same contract the Memory variants satisfy, plus the one thing memory
stores can't prove: data surviving a fresh connection to the same file.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.email_sequence import EmailSequence, EmailSequenceStatus, EmailSequenceStep
from app.repositories.email_sequence_store import EmailSequenceNotFoundError
from app.repositories.sqlite_email_sequence_step_store import SQLiteEmailSequenceStepStore
from app.repositories.sqlite_email_sequence_store import SQLiteEmailSequenceStore


def make_sequence(email_sequence_id: str, campaign_id: str, apollo_sequence_id: str) -> EmailSequence:
    now = datetime.now(timezone.utc)
    return EmailSequence(
        email_sequence_id=email_sequence_id,
        campaign_id=campaign_id,
        apollo_sequence_id=apollo_sequence_id,
        name="Test Sequence",
        status=EmailSequenceStatus.PAUSED,
        created_at=now,
        updated_at=now,
    )


def make_step(email_sequence_id: str, position: int) -> EmailSequenceStep:
    return EmailSequenceStep(
        email_sequence_step_id=f"step-{position}",
        email_sequence_id=email_sequence_id,
        position=position,
        day=position - 1,
        subject=f"Subject {position}",
        body=f"Body {position}",
    )


@pytest_asyncio.fixture
async def sequence_store(tmp_path):
    store = SQLiteEmailSequenceStore(str(tmp_path / "sequences.db"))
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def step_store(tmp_path):
    store = SQLiteEmailSequenceStepStore(str(tmp_path / "steps.db"))
    await store.connect()
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_create_and_get_roundtrip(sequence_store):
    await sequence_store.create(make_sequence("s1", "c1", "apollo-1"))

    fetched = await sequence_store.get("s1")
    assert fetched is not None
    assert fetched.campaign_id == "c1"
    assert fetched.apollo_sequence_id == "apollo-1"


@pytest.mark.asyncio
async def test_get_by_campaign_id(sequence_store):
    await sequence_store.create(make_sequence("s1", "c1", "apollo-1"))

    found = await sequence_store.get_by_campaign_id("c1")
    assert found is not None
    assert found.email_sequence_id == "s1"
    assert await sequence_store.get_by_campaign_id("does-not-exist") is None


@pytest.mark.asyncio
async def test_get_by_apollo_sequence_id(sequence_store):
    await sequence_store.create(make_sequence("s1", "c1", "apollo-1"))

    found = await sequence_store.get_by_apollo_sequence_id("apollo-1")
    assert found is not None
    assert found.email_sequence_id == "s1"
    assert await sequence_store.get_by_apollo_sequence_id("does-not-exist") is None


@pytest.mark.asyncio
async def test_list_all(sequence_store):
    assert await sequence_store.list_all() == []

    await sequence_store.create(make_sequence("s1", "c1", "apollo-1"))
    await sequence_store.create(make_sequence("s2", "c2", "apollo-2"))

    all_sequences = await sequence_store.list_all()
    assert {s.email_sequence_id for s in all_sequences} == {"s1", "s2"}


@pytest.mark.asyncio
async def test_duplicate_campaign_id_raises(sequence_store):
    await sequence_store.create(make_sequence("s1", "c1", "apollo-1"))
    with pytest.raises(ValueError):
        await sequence_store.create(make_sequence("s2", "c1", "apollo-2"))


@pytest.mark.asyncio
async def test_duplicate_apollo_sequence_id_raises(sequence_store):
    await sequence_store.create(make_sequence("s1", "c1", "apollo-1"))
    with pytest.raises(ValueError):
        await sequence_store.create(make_sequence("s2", "c2", "apollo-1"))


@pytest.mark.asyncio
async def test_save_missing_sequence_raises_not_found(sequence_store):
    with pytest.raises(EmailSequenceNotFoundError):
        await sequence_store.save(make_sequence("does-not-exist", "c1", "apollo-1"))


@pytest.mark.asyncio
async def test_save_persists_mutations(sequence_store):
    sequence = make_sequence("s1", "c1", "apollo-1")
    await sequence_store.create(sequence)

    sequence.status = EmailSequenceStatus.ACTIVE
    sequence.unique_opened = 5
    await sequence_store.save(sequence)

    fetched = await sequence_store.get("s1")
    assert fetched.status == EmailSequenceStatus.ACTIVE
    assert fetched.unique_opened == 5


@pytest.mark.asyncio
async def test_sequence_data_survives_a_fresh_connection(tmp_path):
    db_path = str(tmp_path / "persist_test.db")

    first = SQLiteEmailSequenceStore(db_path)
    await first.connect()
    await first.create(make_sequence("s1", "c1", "apollo-1"))
    await first.close()

    second = SQLiteEmailSequenceStore(db_path)
    await second.connect()
    fetched = await second.get_by_campaign_id("c1")
    await second.close()

    assert fetched is not None
    assert fetched.apollo_sequence_id == "apollo-1"


@pytest.mark.asyncio
async def test_step_create_and_list_in_position_order(step_store):
    await step_store.create(make_step("s1", 2))
    await step_store.create(make_step("s1", 1))

    steps = await step_store.list_for_sequence("s1")
    assert [s.position for s in steps] == [1, 2]


@pytest.mark.asyncio
async def test_step_duplicate_position_raises(step_store):
    await step_store.create(make_step("s1", 1))
    with pytest.raises(ValueError):
        await step_store.create(make_step("s1", 1))


@pytest.mark.asyncio
async def test_step_save_updates_apollo_step_id(step_store):
    step = make_step("s1", 1)
    await step_store.create(step)

    step.apollo_step_id = "apollo-step-1"
    await step_store.save(step)

    steps = await step_store.list_for_sequence("s1")
    assert steps[0].apollo_step_id == "apollo-step-1"


@pytest.mark.asyncio
async def test_steps_survive_a_fresh_connection(tmp_path):
    db_path = str(tmp_path / "steps_persist.db")

    first = SQLiteEmailSequenceStepStore(db_path)
    await first.connect()
    await first.create(make_step("s1", 1))
    await first.close()

    second = SQLiteEmailSequenceStepStore(db_path)
    await second.connect()
    steps = await second.list_for_sequence("s1")
    await second.close()

    assert len(steps) == 1
    assert steps[0].subject == "Subject 1"
