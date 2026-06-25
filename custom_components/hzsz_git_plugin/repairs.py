"""Repairs platform — guides user to restart HA after installing components.

Multiple entries accumulate into a single "restart required" repair issue,
so the user only sees one restart prompt regardless of how many repos
they install.
"""

from __future__ import annotations

from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers.issue_registry import async_delete_issue
import voluptuous as vol

from .const import DOMAIN


class RestartRequiredFixFlow(RepairsFlow):
    """Fix flow that offers to restart Home Assistant."""

    def __init__(self, issue_id: str) -> None:
        self.issue_id = issue_id

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the first step."""
        return await self.async_step_confirm_restart()

    async def async_step_confirm_restart(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Ask the user to confirm restart, then restart HA."""
        if user_input is not None:
            # Delete the issue BEFORE restarting so it's gone when
            # the registry is persisted to disk during shutdown.
            async_delete_issue(self.hass, DOMAIN, "restart_required")
            self.hass.data.get(DOMAIN, {}).pop("pending_restart", None)
            await self.hass.services.async_call("homeassistant", "restart")
            return self.async_create_entry(title="", data={})

        # Read the accumulated pending-restart list
        pending: list[str] = self.hass.data.get(DOMAIN, {}).get(
            "pending_restart", []
        )
        repo_list = "\n".join(f"  • {item}" for item in pending) if pending else ""

        return self.async_show_form(
            step_id="confirm_restart",
            data_schema=vol.Schema({}),
            description_placeholders={"name": repo_list},
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None = None,
    *args: Any,
    **kwargs: Any,
) -> RepairsFlow | None:
    """Create fix flow for the single restart-required issue."""
    if issue_id == "restart_required":
        return RestartRequiredFixFlow(issue_id)
    return None
