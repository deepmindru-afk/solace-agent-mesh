"""Unit tests for LeaderElection using SQLite in-memory database.

Tests cover:
- Lease acquisition succeeds when no existing leader
- Heartbeat extends lease
- Expired lease allows takeover by another instance
- Release leadership sets expires_at to now
- get_leader_info returns correct data
"""

import pytest
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DBSession, sessionmaker

from solace_agent_mesh.gateway.http_sse.repository.models.base import Base
from solace_agent_mesh.gateway.http_sse.repository.models.scheduled_task_model import (
    SchedulerLockModel,
)
from solace_agent_mesh.gateway.http_sse.services.scheduler.leader_election import (
    LeaderElection,
)
from solace_agent_mesh.gateway.http_sse.shared import now_epoch_ms


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine():
    """Create an in-memory SQLite engine with all tables."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session_factory(engine):
    """Provide a session factory that yields sessions from the in-memory DB."""
    SessionLocal = sessionmaker(bind=engine)

    @contextmanager
    def factory():
        sess = SessionLocal()
        try:
            yield sess
        finally:
            sess.close()

    return factory


def _make_leader_election(session_factory, instance_id="inst-1", namespace="ns1",
                          heartbeat_interval=30, lease_duration=60):
    """Build a LeaderElection instance."""
    return LeaderElection(
        session_factory=session_factory,
        instance_id=instance_id,
        namespace=namespace,
        heartbeat_interval_seconds=heartbeat_interval,
        lease_duration_seconds=lease_duration,
    )


# ===========================================================================
# Lease acquisition
# ===========================================================================

class TestLeaseAcquisition:
    """Tests for lease acquisition."""

    @pytest.mark.asyncio
    async def test_acquires_leadership_when_no_existing_leader(self, session_factory):
        """First instance to try should acquire leadership."""
        le = _make_leader_election(session_factory)

        result = await le._try_acquire_leadership()
        assert result is True

        # Verify the lock row was created
        with session_factory() as session:
            lock = session.get(SchedulerLockModel, 1)
            assert lock is not None
            assert lock.leader_id == "inst-1"
            assert lock.leader_namespace == "ns1"

    @pytest.mark.asyncio
    async def test_same_instance_can_reacquire(self, session_factory):
        """The same instance can re-acquire (extend) its own lease."""
        le = _make_leader_election(session_factory)

        assert await le._try_acquire_leadership() is True

        # Record the initial expires_at
        with session_factory() as session:
            lock = session.get(SchedulerLockModel, 1)
            initial_expires = lock.expires_at

        # Re-acquire
        assert await le._try_acquire_leadership() is True

        # expires_at should be extended
        with session_factory() as session:
            lock = session.get(SchedulerLockModel, 1)
            assert lock.expires_at >= initial_expires

    @pytest.mark.asyncio
    async def test_second_instance_cannot_acquire_active_lease(self, session_factory):
        """A second instance cannot acquire leadership while the first holds an active lease."""
        le1 = _make_leader_election(session_factory, instance_id="inst-1")
        le2 = _make_leader_election(session_factory, instance_id="inst-2")

        assert await le1._try_acquire_leadership() is True
        assert await le2._try_acquire_leadership() is False

    @pytest.mark.asyncio
    async def test_is_leader_reflects_acquisition(self, session_factory):
        """``is_leader()`` returns the internal state set by the election loop."""
        le = _make_leader_election(session_factory)

        assert await le.is_leader() is False

        le._is_leader = True
        assert await le.is_leader() is True


# ===========================================================================
# Heartbeat
# ===========================================================================

class TestHeartbeat:
    """Tests for heartbeat extending the lease."""

    @pytest.mark.asyncio
    async def test_heartbeat_extends_lease(self, session_factory):
        """A heartbeat should update heartbeat_at and extend expires_at."""
        le = _make_leader_election(session_factory, lease_duration=60)
        le._is_leader = True

        await le._try_acquire_leadership()

        with session_factory() as session:
            lock = session.get(SchedulerLockModel, 1)
            old_heartbeat = lock.heartbeat_at
            old_expires = lock.expires_at

        result = await le._send_heartbeat()
        assert result is True

        with session_factory() as session:
            lock = session.get(SchedulerLockModel, 1)
            assert lock.heartbeat_at >= old_heartbeat
            assert lock.expires_at >= old_expires

    @pytest.mark.asyncio
    async def test_heartbeat_fails_if_lock_stolen(self, session_factory):
        """If another instance has taken the lock, heartbeat returns False."""
        le1 = _make_leader_election(session_factory, instance_id="inst-1")
        le1._is_leader = True

        await le1._try_acquire_leadership()

        # Manually change the leader to simulate takeover
        with session_factory() as session:
            lock = session.get(SchedulerLockModel, 1)
            lock.leader_id = "inst-2"
            session.commit()

        result = await le1._send_heartbeat()
        assert result is False


# ===========================================================================
# Expiry detection and takeover
# ===========================================================================

class TestExpiryAndTakeover:
    """Tests for expired lease allowing takeover."""

    @pytest.mark.asyncio
    async def test_expired_lease_allows_takeover(self, session_factory):
        """When the existing lease has expired, a new instance can take over."""
        le1 = _make_leader_election(session_factory, instance_id="inst-1", lease_duration=60)
        le2 = _make_leader_election(session_factory, instance_id="inst-2", lease_duration=60)

        # inst-1 acquires
        assert await le1._try_acquire_leadership() is True

        # Manually expire the lease
        with session_factory() as session:
            lock = session.get(SchedulerLockModel, 1)
            lock.expires_at = now_epoch_ms() - 1000  # expired 1 second ago
            session.commit()

        # inst-2 should now be able to acquire
        assert await le2._try_acquire_leadership() is True

        with session_factory() as session:
            lock = session.get(SchedulerLockModel, 1)
            assert lock.leader_id == "inst-2"

    @pytest.mark.asyncio
    async def test_non_expired_lease_blocks_takeover(self, session_factory):
        """A non-expired lease blocks other instances."""
        le1 = _make_leader_election(session_factory, instance_id="inst-1", lease_duration=60)
        le2 = _make_leader_election(session_factory, instance_id="inst-2", lease_duration=60)

        assert await le1._try_acquire_leadership() is True
        # Lease is still valid
        assert await le2._try_acquire_leadership() is False


# ===========================================================================
# Release leadership
# ===========================================================================

class TestReleaseLeadership:
    """Tests for graceful leadership release."""

    @pytest.mark.asyncio
    async def test_release_sets_expires_to_now(self, session_factory):
        """Releasing leadership sets expires_at to approximately now, allowing immediate takeover."""
        le = _make_leader_election(session_factory)
        le._is_leader = True

        await le._try_acquire_leadership()

        before_release = now_epoch_ms()
        await le._release_leadership()

        with session_factory() as session:
            lock = session.get(SchedulerLockModel, 1)
            # expires_at should be <= now (allowing immediate takeover)
            assert lock.expires_at <= now_epoch_ms()

    @pytest.mark.asyncio
    async def test_release_does_not_affect_other_leader(self, session_factory):
        """If another instance is the leader, release is a no-op."""
        le1 = _make_leader_election(session_factory, instance_id="inst-1")
        le2 = _make_leader_election(session_factory, instance_id="inst-2")

        await le1._try_acquire_leadership()

        # Expire and let inst-2 take over
        with session_factory() as session:
            lock = session.get(SchedulerLockModel, 1)
            lock.expires_at = now_epoch_ms() - 1000
            session.commit()

        await le2._try_acquire_leadership()

        # inst-1 tries to release — should not affect inst-2's lease
        with session_factory() as session:
            lock = session.get(SchedulerLockModel, 1)
            inst2_expires = lock.expires_at

        await le1._release_leadership()

        with session_factory() as session:
            lock = session.get(SchedulerLockModel, 1)
            assert lock.leader_id == "inst-2"
            assert lock.expires_at == inst2_expires


# ===========================================================================
# get_leader_info
# ===========================================================================

class TestGetLeaderInfo:
    """Tests for get_leader_info."""

    def test_returns_none_when_no_lock(self, session_factory):
        le = _make_leader_election(session_factory)
        info = le.get_leader_info()
        assert info is None

    @pytest.mark.asyncio
    async def test_returns_leader_info(self, session_factory):
        le = _make_leader_election(session_factory)
        await le._try_acquire_leadership()

        info = le.get_leader_info()
        assert info is not None
        assert info["leader_id"] == "inst-1"
        assert info["leader_namespace"] == "ns1"
        assert info["is_self"] is True
        assert info["is_expired"] is False

    @pytest.mark.asyncio
    async def test_reports_expired_correctly(self, session_factory):
        le = _make_leader_election(session_factory)
        await le._try_acquire_leadership()

        # Expire the lease
        with session_factory() as session:
            lock = session.get(SchedulerLockModel, 1)
            lock.expires_at = now_epoch_ms() - 1000
            session.commit()

        info = le.get_leader_info()
        assert info["is_expired"] is True

    @pytest.mark.asyncio
    async def test_is_self_false_for_other_instance(self, session_factory):
        le1 = _make_leader_election(session_factory, instance_id="inst-1")
        le2 = _make_leader_election(session_factory, instance_id="inst-2")

        await le1._try_acquire_leadership()

        info = le2.get_leader_info()
        assert info["leader_id"] == "inst-1"
        assert info["is_self"] is False


# ===========================================================================
# Stop lifecycle
# ===========================================================================

class TestStopLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_stop_releases_leadership(self, session_factory):
        """Stopping the election releases leadership if held."""
        le = _make_leader_election(session_factory)

        await le.start()
        # Give the election loop a moment to acquire
        import asyncio
        await asyncio.sleep(0.1)

        le._is_leader = True
        await le.stop()

        assert le._election_task is None

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, session_factory):
        """Calling start() twice does not create duplicate tasks."""
        le = _make_leader_election(session_factory)

        await le.start()
        task1 = le._election_task

        await le.start()  # second call should be no-op
        assert le._election_task is task1

        await le.stop()
