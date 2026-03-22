"""Unit tests for the scheduled-tasks router.

Focuses on the authorization logic in the
``GET /executions/by-a2a-task/{a2a_task_id}`` endpoint which verifies
``task.created_by == user.get("id")``.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from fastapi import HTTPException

from solace_agent_mesh.gateway.http_sse.routers.scheduled_tasks import (
    get_execution_by_a2a_task_id,
)


def _mock_execution(scheduled_task_id="task-1", a2a_task_id="a2a-123", **overrides):
    """Build a mock ScheduledTaskExecutionModel."""
    execution = MagicMock()
    execution.id = overrides.get("id", "exec-1")
    execution.scheduled_task_id = scheduled_task_id
    execution.a2a_task_id = a2a_task_id
    execution.status = overrides.get("status", "completed")
    execution.scheduled_for = 1700000000000
    execution.started_at = 1700000001000
    execution.completed_at = 1700000010000
    execution.result_summary = None
    execution.error_message = None
    execution.retry_count = 0
    execution.artifacts = None
    execution.notifications_sent = None
    return execution


def _mock_task(task_id="task-1", created_by="owner-user", **overrides):
    """Build a mock ScheduledTaskModel."""
    task = MagicMock()
    task.id = task_id
    task.created_by = created_by
    task.user_id = overrides.get("user_id", created_by)
    task.deleted_at = None
    return task


class TestGetExecutionByA2aTaskId:
    """Tests for the ``get_execution_by_a2a_task_id`` endpoint."""

    @pytest.mark.asyncio
    async def test_returns_execution_when_user_is_owner(self):
        """The endpoint returns the execution when the requesting user owns the parent task."""
        execution = _mock_execution()
        task = _mock_task(created_by="owner-user")

        mock_repo = MagicMock()
        mock_repo.find_execution_by_a2a_task_id.return_value = execution
        mock_repo.find_by_id.return_value = task

        user = {"id": "owner-user", "sub": "owner-user"}
        mock_db = MagicMock()
        mock_scheduler_service = MagicMock()

        with patch(
            "solace_agent_mesh.gateway.http_sse.routers.scheduled_tasks.ScheduledTaskRepository",
            return_value=mock_repo,
        ):
            result = await get_execution_by_a2a_task_id(
                a2a_task_id="a2a-123",
                db=mock_db,
                user=user,
                scheduler_service=mock_scheduler_service,
            )

        assert result.id == "exec-1"
        assert result.a2a_task_id == "a2a-123"

    @pytest.mark.asyncio
    async def test_returns_404_when_user_is_not_owner(self):
        """The endpoint returns 404 (not 403) when the requesting user does NOT own the parent task.

        This prevents confirming existence to unauthorized users.
        """
        execution = _mock_execution()
        task = _mock_task(created_by="owner-user")

        mock_repo = MagicMock()
        mock_repo.find_execution_by_a2a_task_id.return_value = execution
        mock_repo.find_by_id.return_value = task

        user = {"id": "other-user", "sub": "other-user"}
        mock_db = MagicMock()
        mock_scheduler_service = MagicMock()

        with patch(
            "solace_agent_mesh.gateway.http_sse.routers.scheduled_tasks.ScheduledTaskRepository",
            return_value=mock_repo,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_execution_by_a2a_task_id(
                    a2a_task_id="a2a-123",
                    db=mock_db,
                    user=user,
                    scheduler_service=mock_scheduler_service,
                )

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_returns_404_when_execution_not_found(self):
        """The endpoint returns 404 when no execution matches the A2A task ID."""
        mock_repo = MagicMock()
        mock_repo.find_execution_by_a2a_task_id.return_value = None

        user = {"id": "any-user", "sub": "any-user"}
        mock_db = MagicMock()
        mock_scheduler_service = MagicMock()

        with patch(
            "solace_agent_mesh.gateway.http_sse.routers.scheduled_tasks.ScheduledTaskRepository",
            return_value=mock_repo,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_execution_by_a2a_task_id(
                    a2a_task_id="nonexistent",
                    db=mock_db,
                    user=user,
                    scheduler_service=mock_scheduler_service,
                )

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_404_when_parent_task_not_found(self):
        """The endpoint returns 404 when the parent task has been deleted."""
        execution = _mock_execution()

        mock_repo = MagicMock()
        mock_repo.find_execution_by_a2a_task_id.return_value = execution
        mock_repo.find_by_id.return_value = None  # task deleted

        user = {"id": "owner-user", "sub": "owner-user"}
        mock_db = MagicMock()
        mock_scheduler_service = MagicMock()

        with patch(
            "solace_agent_mesh.gateway.http_sse.routers.scheduled_tasks.ScheduledTaskRepository",
            return_value=mock_repo,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_execution_by_a2a_task_id(
                    a2a_task_id="a2a-123",
                    db=mock_db,
                    user=user,
                    scheduler_service=mock_scheduler_service,
                )

        assert exc_info.value.status_code == 404
