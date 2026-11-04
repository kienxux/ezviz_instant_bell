"""Logbook entries for the door being opened from Home Assistant."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_DOOR_OPENED


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, Any]]], None],
) -> None:
    """Say who opened what, from the user of the call that did it."""

    @callback
    def describe(event: Event) -> dict[str, Any]:
        data = event.data
        who = None
        if event.context and event.context.user_id:
            # The logbook shows the user next to the entry; the message only names the lock.
            who = event.context.user_id
        return {
            LOGBOOK_ENTRY_NAME: "EZVIZ intercom",
            LOGBOOK_ENTRY_MESSAGE: f"opened the {data.get('lock', 'door')} from Home Assistant"
            + ("" if who else " (no user: an automation or a script)"),
        }

    async_describe_event(DOMAIN, EVENT_DOOR_OPENED, describe)
