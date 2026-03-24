"""Unit tests for _validate_task_updates in task_builder_assistant.

Tests cover the pure validation/sanitization logic applied to
LLM-generated task_updates dictionaries.
"""

import pytest

from solace_agent_mesh.gateway.http_sse.services.task_builder_assistant import (
    _validate_task_updates,
)


class TestValidateTaskUpdates:
    """Tests for _validate_task_updates."""

    def test_returns_empty_dict_for_non_dict_input(self):
        assert _validate_task_updates(None) == {}
        assert _validate_task_updates("string") == {}
        assert _validate_task_updates(42) == {}
        assert _validate_task_updates([1, 2]) == {}

    def test_filters_disallowed_keys(self):
        raw = {"name": "My Task", "secret_key": "should_be_dropped"}
        result = _validate_task_updates(raw)
        assert "name" in result
        assert "secret_key" not in result

    def test_valid_schedule_type_accepted(self):
        for st in ("cron", "interval", "one_time"):
            result = _validate_task_updates({"schedule_type": st})
            assert result["schedule_type"] == st

    def test_invalid_schedule_type_rejected(self):
        result = _validate_task_updates({"schedule_type": "bogus"})
        assert "schedule_type" not in result

    def test_non_string_schedule_type_rejected(self):
        result = _validate_task_updates({"schedule_type": 123})
        assert "schedule_type" not in result

    def test_valid_target_type_accepted(self):
        for tt in ("agent", "workflow"):
            result = _validate_task_updates({"target_type": tt})
            assert result["target_type"] == tt

    def test_invalid_target_type_rejected(self):
        result = _validate_task_updates({"target_type": "unknown"})
        assert "target_type" not in result

    def test_enabled_coerced_to_bool(self):
        assert _validate_task_updates({"enabled": 1})["enabled"] is True
        assert _validate_task_updates({"enabled": 0})["enabled"] is False
        assert _validate_task_updates({"enabled": ""})["enabled"] is False

    def test_integer_fields_parsed(self):
        result = _validate_task_updates({"max_retries": "3", "timeout_seconds": "120"})
        assert result["max_retries"] == 3
        assert result["timeout_seconds"] == 120

    def test_non_integer_fields_rejected(self):
        result = _validate_task_updates({"max_retries": "abc"})
        assert "max_retries" not in result

    def test_string_fields_truncated_to_500(self):
        long_name = "x" * 1000
        result = _validate_task_updates({"name": long_name})
        assert len(result["name"]) == 500

    def test_non_string_allowed_field_passed_through(self):
        result = _validate_task_updates({"enabled": True})
        assert result["enabled"] is True

    def test_empty_dict_input(self):
        assert _validate_task_updates({}) == {}

    def test_multiple_valid_fields(self):
        raw = {
            "name": "Task",
            "description": "Desc",
            "schedule_type": "cron",
            "target_type": "agent",
            "enabled": True,
            "max_retries": 2,
        }
        result = _validate_task_updates(raw)
        assert result == {
            "name": "Task",
            "description": "Desc",
            "schedule_type": "cron",
            "target_type": "agent",
            "enabled": True,
            "max_retries": 2,
        }
