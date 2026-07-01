"""Tests for try_route_to_text_option's widget classification.

Pins the fix that lets a pasted OAuth login code reach the agent: the
login screen ("Paste code here if prompted >") is a live text field, so
the router must pass it through (caller sends normally) — NOT classify it
as a blocking widget the way it does for PermissionPrompt/ExitPlanMode.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ccbot.handlers.ask_question_router import try_route_to_text_option

LOGIN_PANE = """\
 ✗ Please run /login - APE Error: 401 Invalid authentication credentials

 > /login

 Login

 Browser didn't open? Use the url below to sign in (s is easy)

 https://claude.ai/oauth/authorize?code=true&client_id=abc&state=xyz

 Paste code here if prompted >

 Esc to cancel
"""

PERMISSION_PANE = """\
 Do you want to proceed?

 ❯ 1. Yes
   2. No

 Esc to cancel
"""


class TestTryRouteToTextOption:
    @pytest.mark.asyncio
    async def test_login_prompt_passes_through(self):
        """Login code must NOT be blocked — caller sends it normally."""
        with patch("ccbot.handlers.ask_question_router.session_manager") as sm:
            sm.capture_pane = AsyncMock(return_value=LOGIN_PANE)
            routed, reason = await try_route_to_text_option("@5", "the-auth-code")
            assert routed is False
            assert reason is None  # passthrough → normal send

    @pytest.mark.asyncio
    async def test_other_widget_still_blocks(self):
        """A real menu widget stays blocked (regression guard)."""
        with patch("ccbot.handlers.ask_question_router.session_manager") as sm:
            sm.capture_pane = AsyncMock(return_value=PERMISSION_PANE)
            routed, reason = await try_route_to_text_option("@5", "да")
            assert routed is False
            assert reason is not None and reason.startswith("blocking_widget:")
