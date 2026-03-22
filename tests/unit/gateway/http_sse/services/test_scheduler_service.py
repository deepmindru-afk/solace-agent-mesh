"""Unit tests for SchedulerService.

Tests cover:
- Iterative retry loop in ``_execute_scheduled_task``
- ``_on_lose_leadership`` clearing of running_executions
- ``_monitor_task`` lifecycle (stored on start, cancelled on stop)
- Metadata filtering through ``_SAFE_METADATA_KEYS``
"""

import asyncio
import uuid
from contextlib import contextmanager
from unittest.mock import (
    AsyncMock,
    MagicMock,
    patch,
    PropertyMock,
)

import pytest

from solace_agent_mesh.gateway.http_sse.repository.models.scheduled_task_model import (
    ExecutionStatus,
    ScheduledTaskExecutionModel,
    ScheduledTaskModel,
    ScheduleType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_task(
    task_id="task-1",
    enabled=True,
    deleted_at=None,
    status="active",
    max_retries=0,
    retry_delay_seconds=0,
    timeout_seconds=3600,
    run_count=0,
    consecutive_failure_count=0,
    task_message=None,
    task_metadata=None,
    target_agent_name="agent-a",
    user_id="user-1",
    created_by="user-1",
    name="test-task",
):
    """Build a mock ScheduledTaskModel."""
    task = MagicMock(spec=ScheduledTaskModel)
    task.id = task_id
    task.name = name
    task.enabled = enabled
    task.deleted_at = deleted_at
    task.status = status
    task.max_retries = max_retries
    task.retry_delay_seconds = retry_delay_seconds
    task.timeout_seconds = timeout_seconds
    task.run_count = run_count
    task.consecutive_failure_count = consecutive_failure_count
    task.task_message = task_message or [{"type": "text", "text": "hello"}]
    task.task_metadata = task_metadata
    task.target_agent_name = target_agent_name
    task.user_id = user_id
    task.created_by = created_by
    task.schedule_type = ScheduleType.CRON
    task.schedule_expression = "*/5 * * * *"
    task.timezone = "UTC"
    task.namespace = "ns1"
    return task


def _make_mock_execution(
    execution_id=None,
    status=ExecutionStatus.PENDING,
    a2a_task_id=None,
):
    """Build a mock ScheduledTaskExecutionModel."""
    execution = MagicMock(spec=ScheduledTaskExecutionModel)
    execution.id = execution_id or str(uuid.uuid4())
    execution.status = status
    execution.a2a_task_id = a2a_task_id
    execution.started_at = None
    execution.completed_at = None
    execution.error_message = None
    return execution


def _build_scheduler_service(**overrides):
    """Build a SchedulerService with mocked dependencies.

    Returns (service, mocks_dict) where mocks_dict contains the key mocks.
    """
    from solace_agent_mesh.gateway.http_sse.services.scheduler.scheduler_service import (
        SchedulerService,
    )

    mock_session = MagicMock()
    mock_session.get = MagicMock()
    mock_session.add = MagicMock()
    mock_session.commit = MagicMock()
    mock_session.flush = MagicMock()
    mock_session.execute = MagicMock()

    @contextmanager
    def mock_session_factory():
        yield mock_session

    mock_publish = MagicMock()
    mock_core_a2a = MagicMock()

    config = overrides.get("config", {})

    with patch(
        "solace_agent_mesh.gateway.http_sse.services.scheduler.scheduler_service.LeaderElection"
    ) as MockLeaderElection, patch(
        "solace_agent_mesh.gateway.http_sse.services.scheduler.scheduler_service.ResultHandler"
    ) as MockResultHandler, patch(
        "solace_agent_mesh.gateway.http_sse.services.scheduler.scheduler_service.NotificationService"
    ) as MockNotificationService, patch(
        "solace_agent_mesh.gateway.http_sse.services.scheduler.scheduler_service.AsyncIOScheduler"
    ) as MockScheduler:
        mock_leader = MockLeaderElection.return_value
        mock_leader.start = AsyncMock()
        mock_leader.stop = AsyncMock()
        mock_leader.is_leader = AsyncMock(return_value=True)
        mock_leader._is_leader = True

        mock_result_handler = MockResultHandler.return_value
        mock_result_handler.register_execution = AsyncMock()
        mock_result_handler.wait_for_completion = AsyncMock()

        mock_notification = MockNotificationService.return_value
        mock_notification.cleanup = AsyncMock()
        mock_notification.notify_execution_complete = AsyncMock()

        mock_scheduler_instance = MockScheduler.return_value
        mock_scheduler_instance.start = MagicMock()
        mock_scheduler_instance.shutdown = MagicMock()
        mock_scheduler_instance.add_job = MagicMock()
        mock_scheduler_instance.remove_job = MagicMock()
        mock_scheduler_instance.running = True

        service = SchedulerService(
            session_factory=mock_session_factory,
            namespace="ns1",
            instance_id="inst-1",
            publish_func=mock_publish,
            core_a2a_service=mock_core_a2a,
            config=config,
        )

    return service, {
        "session": mock_session,
        "publish": mock_publish,
        "leader_election": service.leader_election,
        "result_handler": service.result_handler,
        "notification_service": service.notification_service,
        "scheduler": service.scheduler,
    }


# ===========================================================================
# Retry loop
# ===========================================================================

class TestRetryLoop:
    """Tests for the iterative retry loop in ``_execute_scheduled_task``."""

    @pytest.mark.asyncio
    async def test_retry_loop_runs_correct_number_of_attempts(self):
        """With max_retries=2, the loop should attempt up to 3 times (1 initial + 2 retries)."""
        service, mocks = _build_scheduler_service()

        task = _make_mock_task(max_retries=2, retry_delay_seconds=0, timeout_seconds=10)

        # Track how many times _submit_task_to_agent_mesh is called
        submit_call_count = 0

        async def mock_submit(task_id, execution_id):
            nonlocal submit_call_count
            submit_call_count += 1
            raise Exception("Simulated failure")

        service._submit_task_to_agent_mesh = mock_submit

        # session.get returns the task on first call, then task for each retry
        mocks["session"].get.return_value = task

        await service._execute_scheduled_task("task-1")

        # 1 initial + 2 retries = 3 attempts
        assert submit_call_count == 3

    @pytest.mark.asyncio
    async def test_retry_loop_stops_on_success(self):
        """If the first attempt succeeds, no retries are made."""
        service, mocks = _build_scheduler_service()

        task = _make_mock_task(max_retries=3, retry_delay_seconds=0, timeout_seconds=10)

        submit_call_count = 0

        # Build a completed execution mock
        completed_execution = _make_mock_execution(status=ExecutionStatus.COMPLETED)

        async def mock_submit(task_id, execution_id):
            nonlocal submit_call_count
            submit_call_count += 1
            # Simulate successful completion

        def mock_session_get(model_or_id, id_val=None):
            if id_val is None:
                # Called as session.get(Model, id)
                return task
            # Called with two args
            if isinstance(model_or_id, type) and model_or_id == ScheduledTaskExecutionModel:
                return completed_execution
            return task

        mocks["session"].get.side_effect = lambda model, id_val=None: (
            completed_execution if model == ScheduledTaskExecutionModel or (isinstance(model, str) and model != "task-1") else task
        )

        # Simplify: just make session.get always return the right thing
        call_count = [0]

        def smart_get(model_cls, obj_id=None):
            if obj_id is None:
                obj_id = model_cls
                # single arg call
                return task
            if model_cls == ScheduledTaskExecutionModel:
                return completed_execution
            return task

        mocks["session"].get.side_effect = smart_get

        service._submit_task_to_agent_mesh = AsyncMock()

        await service._execute_scheduled_task("task-1")

        # Only 1 attempt since it succeeded
        assert service._submit_task_to_agent_mesh.call_count == 1

    @pytest.mark.asyncio
    async def test_no_retries_when_max_retries_is_zero(self):
        """With max_retries=0, only one attempt is made."""
        service, mocks = _build_scheduler_service()

        task = _make_mock_task(max_retries=0, retry_delay_seconds=0, timeout_seconds=10)
        mocks["session"].get.return_value = task

        submit_call_count = 0

        async def mock_submit(task_id, execution_id):
            nonlocal submit_call_count
            submit_call_count += 1
            raise Exception("Simulated failure")

        service._submit_task_to_agent_mesh = mock_submit

        await service._execute_scheduled_task("task-1")

        assert submit_call_count == 1

    @pytest.mark.asyncio
    async def test_skips_execution_when_task_not_found(self):
        """If the task is not found in the DB, execution is skipped entirely."""
        service, mocks = _build_scheduler_service()

        mocks["session"].get.return_value = None

        service._submit_task_to_agent_mesh = AsyncMock()

        await service._execute_scheduled_task("nonexistent")

        service._submit_task_to_agent_mesh.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_execution_when_task_in_error_state(self):
        """Tasks in error state are skipped."""
        service, mocks = _build_scheduler_service()

        # First call returns task for config read, second returns error-state task
        task_config = _make_mock_task(status="active")
        task_error = _make_mock_task(status="error")

        call_count = [0]

        def get_side_effect(model, obj_id=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return task_config  # config read
            return task_error  # execution check

        mocks["session"].get.side_effect = get_side_effect

        service._submit_task_to_agent_mesh = AsyncMock()

        await service._execute_scheduled_task("task-1")

        service._submit_task_to_agent_mesh.assert_not_called()


# ===========================================================================
# _on_lose_leadership
# ===========================================================================

class TestOnLoseLeadership:
    """Tests for ``_on_lose_leadership`` clearing running_executions."""

    @pytest.mark.asyncio
    async def test_clears_running_executions(self):
        """Losing leadership clears the running_executions dict."""
        service, mocks = _build_scheduler_service()

        # Simulate some running executions
        service.running_executions = {
            "exec-1": MagicMock(),
            "exec-2": MagicMock(),
        }

        # Mock _unload_all_tasks to avoid side effects
        service._unload_all_tasks = AsyncMock()

        await service._on_lose_leadership()

        assert len(service.running_executions) == 0

    @pytest.mark.asyncio
    async def test_calls_unload_all_tasks(self):
        """Losing leadership also unloads all scheduled tasks."""
        service, mocks = _build_scheduler_service()
        service._unload_all_tasks = AsyncMock()

        await service._on_lose_leadership()

        service._unload_all_tasks.assert_awaited_once()


# ===========================================================================
# stop() lifecycle
# ===========================================================================

class TestStopLifecycle:
    """Tests for the ``stop()`` method lifecycle."""

    @pytest.mark.asyncio
    async def test_stop_cancels_monitor_task(self):
        """``stop()`` cancels the ``_monitor_task`` if it's running."""
        service, mocks = _build_scheduler_service()

        # Create a real asyncio task that we can check
        async def long_running():
            await asyncio.sleep(3600)

        service._monitor_task = asyncio.create_task(long_running())
        service._stale_cleanup_task = asyncio.create_task(long_running())

        await service.stop()

        assert service._monitor_task.cancelled() or service._monitor_task.done()
        assert service._stale_cleanup_task.cancelled() or service._stale_cleanup_task.done()

    @pytest.mark.asyncio
    async def test_stop_cancels_running_executions(self):
        """``stop()`` cancels all running execution tasks."""
        service, mocks = _build_scheduler_service()

        async def long_running():
            await asyncio.sleep(3600)

        exec_task = asyncio.create_task(long_running())
        service.running_executions = {"exec-1": exec_task}
        service._monitor_task = None
        service._stale_cleanup_task = None

        await service.stop()

        # Allow the event loop to process the cancellation
        await asyncio.sleep(0)
        try:
            await exec_task
        except asyncio.CancelledError:
            pass

        assert exec_task.cancelled() or exec_task.done()

    @pytest.mark.asyncio
    async def test_start_creates_monitor_task(self):
        """``start()`` creates the ``_monitor_task``."""
        service, mocks = _build_scheduler_service()

        assert service._monitor_task is None

        await service.start()

        assert service._monitor_task is not None
        assert not service._monitor_task.done()

        # Cleanup
        service._monitor_task.cancel()
        if service._stale_cleanup_task:
            service._stale_cleanup_task.cancel()
        try:
            await service._monitor_task
        except asyncio.CancelledError:
            pass
        try:
            if service._stale_cleanup_task:
                await service._stale_cleanup_task
        except asyncio.CancelledError:
            pass


# ===========================================================================
# Metadata filtering
# ===========================================================================

class TestMetadataFiltering:
    """Tests for ``_SAFE_METADATA_KEYS`` filtering in ``_submit_task_to_agent_mesh``."""

    def test_safe_metadata_keys_constant(self):
        """Verify the set of safe metadata keys."""
        from solace_agent_mesh.gateway.http_sse.services.scheduler.scheduler_service import (
            _SAFE_METADATA_KEYS,
        )

        assert "priority" in _SAFE_METADATA_KEYS
        assert "tags" in _SAFE_METADATA_KEYS
        assert "category" in _SAFE_METADATA_KEYS
        assert "source" in _SAFE_METADATA_KEYS
        # Protocol-level keys must NOT be in the safe set
        assert "sessionBehavior" not in _SAFE_METADATA_KEYS
        assert "returnArtifacts" not in _SAFE_METADATA_KEYS
        assert "replyTo" not in _SAFE_METADATA_KEYS

    @pytest.mark.asyncio
    async def test_metadata_filtering_strips_non_safe_keys(self):
        """Non-safe keys in task_metadata are stripped before building the A2A message."""
        service, mocks = _build_scheduler_service()

        task_metadata = {
            "priority": "high",
            "tags": ["daily"],
            "dangerous_key": "should-be-stripped",
            "sessionBehavior": "OVERRIDE_ATTEMPT",
        }

        task = _make_mock_task(
            task_metadata=task_metadata,
            task_message=[{"type": "text", "text": "test"}],
        )

        # We'll capture the payload passed to publish_func
        captured_payloads = []
        captured_user_props = []

        def capture_publish(topic, payload, user_props):
            captured_payloads.append(payload)
            captured_user_props.append(user_props)

        service.publish_func = capture_publish

        mocks["session"].get.return_value = task

        with patch(
            "solace_agent_mesh.gateway.http_sse.services.scheduler.scheduler_service.a2a"
        ) as mock_a2a:
            mock_a2a.create_text_part.return_value = {"type": "text", "text": "test"}
            mock_a2a.create_user_message.return_value = MagicMock()

            mock_request = MagicMock()
            mock_request.model_dump.return_value = {"test": "payload"}
            mock_a2a.create_send_streaming_message_request.return_value = mock_request
            mock_a2a.get_agent_request_topic.return_value = "ns1/agent-a"

            # Capture the metadata arg passed to create_user_message
            captured_metadata = []

            def capture_create_user_message(**kwargs):
                captured_metadata.append(kwargs.get("metadata", {}))
                return MagicMock()

            mock_a2a.create_user_message.side_effect = capture_create_user_message

            # Also capture metadata passed to create_send_streaming_message_request
            captured_request_metadata = []

            def capture_create_request(**kwargs):
                captured_request_metadata.append(kwargs.get("metadata", {}))
                return mock_request

            mock_a2a.create_send_streaming_message_request.side_effect = capture_create_request

            await service._submit_task_to_agent_mesh("task-1", "exec-1")

        # Verify the message metadata has safe keys + protocol keys, but not dangerous ones
        assert len(captured_metadata) == 1
        msg_meta = captured_metadata[0]
        assert msg_meta.get("priority") == "high"
        assert msg_meta.get("tags") == ["daily"]
        assert "dangerous_key" not in msg_meta
        assert msg_meta.get("sessionBehavior") == "RUN_BASED"  # protocol override
        assert msg_meta.get("returnArtifacts") is True

        # Verify the request-level metadata also filters
        assert len(captured_request_metadata) == 1
        req_meta = captured_request_metadata[0]
        assert "dangerous_key" not in req_meta
        assert "sessionBehavior" not in req_meta  # not a safe key for request metadata


# ===========================================================================
# Template rendering
# ===========================================================================

class TestTemplateRendering:
    """Tests for ``_render_template_variables_from_fields``."""

    def test_renders_all_template_variables(self):
        service, _ = _build_scheduler_service()

        text = "Task: {{schedule.name}}, Run: {{schedule.run_count}}, Exec: {{execution.id}}, Date: {{schedule.run_date}}"
        result = service._render_template_variables_from_fields(
            text, "My Task", 42, "exec-abc"
        )

        assert "My Task" in result
        assert "42" in result
        assert "exec-abc" in result
        assert "{{" not in result  # all placeholders replaced

    def test_handles_none_task_name(self):
        service, _ = _build_scheduler_service()

        text = "Task: {{schedule.name}}"
        result = service._render_template_variables_from_fields(text, None, 0, "exec-1")
        assert "Task: " in result
        assert "None" not in result
