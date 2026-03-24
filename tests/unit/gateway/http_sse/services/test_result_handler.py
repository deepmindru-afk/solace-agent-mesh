"""Unit tests for ResultHandler and _sanitize_error_message.

Tests cover:
- ``_sanitize_error_message`` edge cases (empty, multi-line, truncation, whitespace)
- ``_handle_success`` DB update and event signalling
- ``_handle_error`` DB update, event signalling, and log sanitization
"""

import asyncio
from contextlib import contextmanager
from unittest.mock import (
    AsyncMock,
    MagicMock,
    patch,
)

import pytest

from a2a.types import Task, JSONRPCError
from solace_agent_mesh.gateway.http_sse.services.scheduler.result_handler import (
    ResultHandler,
    _sanitize_error_message,
    _MAX_USER_ERROR_LENGTH,
)
from solace_agent_mesh.gateway.http_sse.repository.models import ExecutionStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_result_handler():
    """Build a ResultHandler with a mocked session factory.

    Returns (handler, mock_session).
    """
    mock_session = MagicMock()
    mock_session.commit = MagicMock()
    mock_session.add = MagicMock()

    @contextmanager
    def mock_session_factory():
        yield mock_session

    handler = ResultHandler(
        session_factory=mock_session_factory,
        namespace="ns1",
        instance_id="inst-1",
    )
    return handler, mock_session


# ===========================================================================
# _sanitize_error_message
# ===========================================================================

class TestSanitizeErrorMessage:
    """Tests for the module-level ``_sanitize_error_message`` function."""

    def test_none_returns_default(self):
        assert _sanitize_error_message(None) == "Task execution failed"

    def test_empty_string_returns_default(self):
        assert _sanitize_error_message("") == "Task execution failed"

    def test_multiline_returns_first_line(self):
        msg = "First line\nSecond line\nThird line"
        assert _sanitize_error_message(msg) == "First line"

    def test_long_input_truncated_with_ellipsis(self):
        long_msg = "a" * (_MAX_USER_ERROR_LENGTH + 100)
        result = _sanitize_error_message(long_msg)
        assert len(result) == _MAX_USER_ERROR_LENGTH + 3  # +3 for "..."
        assert result.endswith("...")

    def test_short_single_line_returned_as_is(self):
        msg = "Something went wrong"
        assert _sanitize_error_message(msg) == "Something went wrong"

    def test_whitespace_only_first_line_returns_default(self):
        msg = "   \nActual error on second line"
        assert _sanitize_error_message(msg) == "Task execution failed"


# ===========================================================================
# _handle_success
# ===========================================================================

class TestHandleSuccess:
    """Tests for ``ResultHandler._handle_success``."""

    @pytest.mark.asyncio
    async def test_task_result_updates_execution_and_signals_event(self):
        """A Task result with status.message should update the DB and signal completion."""
        handler, mock_session = _build_result_handler()

        execution_id = "exec-1"

        # Set up completion event
        event = asyncio.Event()
        handler.completion_events[execution_id] = event
        handler.execution_sessions[execution_id] = "session-123"

        # Build a Task result with a status message
        mock_message = MagicMock()
        mock_status = MagicMock()
        mock_status.message = mock_message
        task_result = MagicMock(spec=Task)
        task_result.status = mock_status

        mock_repo = MagicMock()
        mock_repo.find_execution_by_id.return_value = None  # skip _save_chat_task

        with patch(
            "solace_agent_mesh.gateway.http_sse.services.scheduler.result_handler.ScheduledTaskRepository",
            return_value=mock_repo,
        ), patch(
            "solace_agent_mesh.gateway.http_sse.services.scheduler.result_handler.a2a"
        ) as mock_a2a, patch(
            "solace_agent_mesh.gateway.http_sse.services.scheduler.result_handler.now_epoch_ms",
            return_value=1000,
        ):
            mock_a2a.get_text_from_message.return_value = "Agent says hello"
            mock_a2a.get_file_parts_from_message.return_value = []
            mock_a2a.get_task_history.return_value = []
            mock_a2a.get_task_status.return_value = "completed"
            mock_a2a.get_task_metadata.return_value = None
            mock_a2a.get_task_artifacts.return_value = []

            await handler._handle_success(execution_id, task_result)

        # Verify DB update
        mock_repo.update_execution.assert_called_once()
        call_args = mock_repo.update_execution.call_args
        assert call_args[0][0] is mock_session  # session
        assert call_args[0][1] == execution_id
        update_data = call_args[0][2]
        assert update_data["status"] == ExecutionStatus.COMPLETED
        assert "agent_response" in update_data["result_summary"]
        assert update_data["result_summary"]["agent_response"] == "Agent says hello"

        # Verify event was signalled
        assert event.is_set()

        # Verify execution_sessions cleaned up
        assert execution_id not in handler.execution_sessions

    @pytest.mark.asyncio
    async def test_non_task_result_completes_without_error(self):
        """A non-Task result should still complete and signal the event."""
        handler, mock_session = _build_result_handler()

        execution_id = "exec-2"
        event = asyncio.Event()
        handler.completion_events[execution_id] = event

        non_task_result = {"some": "dict"}  # Not a Task instance

        mock_repo = MagicMock()
        mock_repo.find_execution_by_id.return_value = None

        with patch(
            "solace_agent_mesh.gateway.http_sse.services.scheduler.result_handler.ScheduledTaskRepository",
            return_value=mock_repo,
        ), patch(
            "solace_agent_mesh.gateway.http_sse.services.scheduler.result_handler.now_epoch_ms",
            return_value=2000,
        ):
            await handler._handle_success(execution_id, non_task_result)

        # Should still complete
        mock_repo.update_execution.assert_called_once()
        update_data = mock_repo.update_execution.call_args[0][2]
        assert update_data["status"] == ExecutionStatus.COMPLETED

        # Event signalled
        assert event.is_set()


# ===========================================================================
# _handle_error
# ===========================================================================

class TestHandleError:
    """Tests for ``ResultHandler._handle_error``."""

    @pytest.mark.asyncio
    async def test_error_updates_execution_with_sanitized_message(self):
        """A JSONRPCError should update the DB with FAILED status and sanitized error."""
        handler, mock_session = _build_result_handler()

        execution_id = "exec-3"
        event = asyncio.Event()
        handler.completion_events[execution_id] = event

        error = MagicMock(spec=JSONRPCError)
        error.message = "Something failed"
        error.code = -32000

        mock_repo = MagicMock()
        mock_repo.find_execution_by_id.return_value = None

        with patch(
            "solace_agent_mesh.gateway.http_sse.services.scheduler.result_handler.ScheduledTaskRepository",
            return_value=mock_repo,
        ), patch(
            "solace_agent_mesh.gateway.http_sse.services.scheduler.result_handler.now_epoch_ms",
            return_value=3000,
        ):
            await handler._handle_error(execution_id, error)

        mock_repo.update_execution.assert_called_once()
        call_args = mock_repo.update_execution.call_args
        update_data = call_args[0][2]
        assert update_data["status"] == ExecutionStatus.FAILED
        assert update_data["error_message"] == "Something failed"
        assert update_data["result_summary"] == {"error_code": -32000}

        # Event signalled
        assert event.is_set()

    @pytest.mark.asyncio
    async def test_multiline_error_is_sanitized_in_db_and_log(self):
        """A multi-line error with stack trace content should be sanitized."""
        handler, mock_session = _build_result_handler()

        execution_id = "exec-4"
        event = asyncio.Event()
        handler.completion_events[execution_id] = event

        raw_message = (
            "NullPointerException: something broke\n"
            "  at com.example.Foo.bar(Foo.java:42)\n"
            "  at com.example.Main.main(Main.java:10)"
        )
        error = MagicMock(spec=JSONRPCError)
        error.message = raw_message
        error.code = -32603

        mock_repo = MagicMock()
        mock_repo.find_execution_by_id.return_value = None

        with patch(
            "solace_agent_mesh.gateway.http_sse.services.scheduler.result_handler.ScheduledTaskRepository",
            return_value=mock_repo,
        ), patch(
            "solace_agent_mesh.gateway.http_sse.services.scheduler.result_handler.now_epoch_ms",
            return_value=4000,
        ), patch(
            "solace_agent_mesh.gateway.http_sse.services.scheduler.result_handler.log"
        ) as mock_log:
            await handler._handle_error(execution_id, error)

        # DB should have sanitized (first line only) error
        update_data = mock_repo.update_execution.call_args[0][2]
        assert update_data["error_message"] == "NullPointerException: something broke"
        assert "\n" not in update_data["error_message"]

        # The raw multi-line error should NOT appear in log calls
        for call in mock_log.warning.call_args_list:
            formatted = call[0][0] % call[0][1:] if len(call[0]) > 1 else call[0][0]
            assert "Foo.java:42" not in formatted
            assert "Main.java:10" not in formatted
