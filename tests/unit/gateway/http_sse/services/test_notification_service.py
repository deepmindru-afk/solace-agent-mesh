"""Unit tests for _format_webhook_payload in NotificationService.

Tests the pure formatting logic for generic, Slack, and Teams webhook
payloads — no mocks or I/O required.
"""

import pytest

from solace_agent_mesh.gateway.http_sse.services.scheduler.notification_service import (
    NotificationService,
)


def _make_service():
    """Create a NotificationService with dummy dependencies (not used by the pure method)."""
    return NotificationService(
        session_factory=None,
        sse_manager=None,
        publish_func=None,
        namespace="test",
        instance_id="test-0",
    )


_SAMPLE_PAYLOAD = {
    "task_id": "t1",
    "task_name": "Daily Report",
    "execution_id": "e1",
    "status": "completed",
    "scheduled_for": 1700000000000,
    "started_at": 1700000001000,
    "completed_at": 1700000010000,
    "namespace": "default",
    "user_id": "u1",
}


class TestFormatWebhookPayload:
    """Tests for NotificationService._format_webhook_payload."""

    def test_generic_returns_payload_unchanged(self):
        svc = _make_service()
        result = svc._format_webhook_payload(_SAMPLE_PAYLOAD, {"webhook_type": "generic"})
        assert result is _SAMPLE_PAYLOAD

    def test_default_type_is_generic(self):
        svc = _make_service()
        result = svc._format_webhook_payload(_SAMPLE_PAYLOAD, {})
        assert result is _SAMPLE_PAYLOAD

    def test_slack_format_contains_blocks(self):
        svc = _make_service()
        result = svc._format_webhook_payload(_SAMPLE_PAYLOAD, {"webhook_type": "slack"})
        assert "text" in result
        assert "blocks" in result
        assert "Daily Report" in result["text"]
        assert result["blocks"][0]["type"] == "section"

    def test_slack_format_failed_status(self):
        svc = _make_service()
        payload = {**_SAMPLE_PAYLOAD, "status": "failed"}
        result = svc._format_webhook_payload(payload, {"webhook_type": "slack"})
        assert "fail" in result["text"]

    def test_teams_format_contains_sections(self):
        svc = _make_service()
        result = svc._format_webhook_payload(_SAMPLE_PAYLOAD, {"webhook_type": "teams"})
        assert result["@type"] == "MessageCard"
        assert "sections" in result
        assert result["themeColor"] == "00FF00"

    def test_teams_format_failed_uses_red(self):
        svc = _make_service()
        payload = {**_SAMPLE_PAYLOAD, "status": "failed"}
        result = svc._format_webhook_payload(payload, {"webhook_type": "teams"})
        assert result["themeColor"] == "FF0000"
