"""Config flow: the camera's pairing QR (a picker when it holds several cameras); reauth = fresh QR.

Credentials come from the pairing-QR text (docs/vtech-lan-kit/docs/05-provisioning.md); the QR text itself is
never stored.
"""
from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from .const import (
    CONF_ACCOUNT,
    CONF_AUTHKEY,
    CONF_AV_PASSWORD,
    CONF_MAC,
    CONF_PLAIN,
    CONF_QR,
    CONF_UID,
    DOMAIN,
    TESTED_MODELS,
    TESTED_PRODUCT,
)
from .manager import SetupError, async_load, async_probe
from .vtech_lan import provision
from .vtech_lan.errors import AuthFailed, Unreachable, VtechError
from .vtech_lan.hub import Hub

_LOG = logging.getLogger(__name__)

QR_SCHEMA = vol.Schema({vol.Required(CONF_QR): TextSelector(TextSelectorConfig(multiline=True))})


class VtechBabyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._hub: Hub | None = None
        self._model: str | None = None                    # from the connection test
        self._cameras: list[provision.Credentials] = []   # a QR's cameras not yet configured, to pick from
        self._pending: tuple[str, dict[str, Any]] | None = None   # entry awaiting the model warning

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_camera()

    # The pairing QR; a QR with several cameras goes on to the picker
    async def async_step_camera(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            found, errors = self._decode(user_input[CONF_QR])
            if not errors:
                configured = self._async_current_ids()
                self._cameras = [c for c in found if c.uid not in configured]
                if not self._cameras:
                    return self.async_abort(reason="already_configured")   # rotated credentials: reauth
                if len(self._cameras) > 1:
                    return await self.async_step_pick_camera()
                result, errors = await self._async_add(self._cameras[0])
                if result is not None:
                    return result
        return self.async_show_form(step_id="camera", data_schema=QR_SCHEMA, errors=errors)

    async def async_step_pick_camera(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            creds = next(c for c in self._cameras if c.uid == user_input[CONF_UID])
            result, errors = await self._async_add(creds)
            if result is not None:
                return result
        choices = {c.uid: f"{c.name or 'VTech'} ({c.uid[-4:]})" for c in self._cameras}
        return self.async_show_form(step_id="pick_camera", errors=errors,
                                    data_schema=vol.Schema({vol.Required(CONF_UID): vol.In(choices)}))

    async def _async_add(self, creds: provision.Credentials
                         ) -> tuple[ConfigFlowResult | None, dict[str, str]]:
        """Test one camera and create its entry (after the model warning, if needed). Returns the flow
        result, or the form errors."""
        await self.async_set_unique_id(creds.uid)
        self._abort_if_unique_id_configured()
        creds, errors = await self._async_test(creds)
        if errors:
            return None, errors
        self._pending = (creds.name or f"VTech {creds.uid[-4:]}", _entry_creds(creds))
        if (self._model or "").strip().upper() not in TESTED_MODELS:
            return await self.async_step_untested_model(), {}
        return self.async_create_entry(title=self._pending[0], data=self._pending[1]), {}

    # Only after a successful connection whose model isn't one we've tested: warn, let the user go on.
    async def async_step_untested_model(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._pending is not None
        if user_input is not None:
            return self.async_create_entry(title=self._pending[0], data=self._pending[1])
        _LOG.warning("camera %s reports model %r, which this integration has not been tested with",
                     self._pending[0], self._model)
        return self.async_show_form(step_id="untested_model", data_schema=vol.Schema({}),
                                    description_placeholders={"model": self._model or "unknown",
                                                              "tested": TESTED_PRODUCT})

    # Reauth: a fresh QR holding the same camera
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            found, errors = self._decode(user_input[CONF_QR])
            creds = next((c for c in found if c.uid == entry.unique_id), None)
            if not errors and creds is None:
                errors = {CONF_QR: "wrong_camera"}
            if creds is not None:
                creds, errors = await self._async_test(creds)
            if not errors:
                assert creds is not None
                return self.async_update_reload_and_abort(entry, data_updates=_entry_creds(creds))
        return self.async_show_form(step_id="reauth_confirm", data_schema=QR_SCHEMA, errors=errors,
                                    description_placeholders={"name": entry.title})

    @staticmethod
    def _decode(text: str) -> tuple[list[provision.Credentials], dict[str, str]]:
        try:
            return provision.cameras(text), {}
        except provision.UnsupportedModel as err:
            _LOG.debug("pairing QR: %s", err)
            return [], {CONF_QR: "unsupported_model"}
        except provision.InvalidQr as err:
            _LOG.debug("invalid pairing QR: %s", err)
            return [], {CONF_QR: "invalid_qr"}

    async def _async_test(self, creds: provision.Credentials
                          ) -> tuple[provision.Credentials, dict[str, str]]:
        """Connect once with the decoded credentials. Returns them (marked plain if the camera turned out
        to be), and form errors (empty = OK)."""
        if self._hub is None:
            try:
                self._hub = await async_load(self.hass)
            except SetupError as err:
                _LOG.warning("the protocol module is not usable: %s", err)
                return creds, {"base": "module_unavailable"}
        try:
            dev, plain = await async_probe(self.hass, self._hub, _entry_creds(creds))
        except AuthFailed:
            return creds, {"base": "invalid_auth"}
        except Unreachable:
            return creds, {"base": "cannot_connect"}
        except VtechError:
            _LOG.exception("unexpected error testing the camera connection")
            return creds, {"base": "unknown"}
        self._model = dev.model
        return dataclasses.replace(creds, plain=plain), {}


def _entry_creds(creds: provision.Credentials) -> dict[str, Any]:
    return {CONF_UID: creds.uid, CONF_ACCOUNT: creds.account, CONF_AV_PASSWORD: creds.av_password,
            CONF_AUTHKEY: creds.authkey, CONF_MAC: creds.mac, CONF_PLAIN: creds.plain}
