"""Config flow for HZSZ_IOT_001 Thing Model.

Collects MQTT broker connection details from the user during initial setup.
The configuration is stored in the config entry and persists until the
integration is removed from Home Assistant.
"""

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback

from .const import (
    DEFAULT_MQTT_BROKER,
    DEFAULT_MQTT_PASSWORD,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_USERNAME,
    DOMAIN,
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_MQTT_BROKER): str,
        vol.Required(CONF_PORT, default=DEFAULT_MQTT_PORT): int,
        vol.Required(CONF_USERNAME, default=DEFAULT_MQTT_USERNAME): str,
        vol.Required(CONF_PASSWORD, default=DEFAULT_MQTT_PASSWORD): str,
    }
)


class HzszIotConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HZSZ_IOT_001 Thing Model.

    Presents a form to collect MQTT broker host, port, username, and password.
    These values are stored in the config entry data and used every time
    Home Assistant starts.
    """

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step — collect MQTT connection details.

        When user_input is None, show the form with default values.
        When user_input is provided, validate and create the config entry.
        """
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="HZSZ_IOT_001 (Thing Model)",
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return HzszIotOptionsFlow(config_entry)


class HzszIotOptionsFlow(config_entries.OptionsFlow):
    """Handle reconfiguration of MQTT settings from the integration options.

    Updates the config entry ``data`` directly so credentials stay in the
    encrypted storage rather than in unencrypted ``options``.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options — allow changing MQTT connection details."""
        if user_input is not None:
            # Update config entry data in-place so credentials remain in
            # encrypted storage. The integration must be reloaded for the
            # new settings to take effect.
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data=user_input,
            )
            await self.hass.config_entries.async_reload(
                self._config_entry.entry_id
            )
            return self.async_create_entry(data={})

        # Pre-fill with current values from config entry data
        current = self._config_entry.data

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_HOST,
                    default=current.get(CONF_HOST, DEFAULT_MQTT_BROKER),
                ): str,
                vol.Required(
                    CONF_PORT,
                    default=current.get(CONF_PORT, DEFAULT_MQTT_PORT),
                ): int,
                vol.Required(
                    CONF_USERNAME,
                    default=current.get(CONF_USERNAME, DEFAULT_MQTT_USERNAME),
                ): str,
                vol.Required(
                    CONF_PASSWORD,
                    default=current.get(CONF_PASSWORD, DEFAULT_MQTT_PASSWORD),
                ): str,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
        )
