"""Config flow: the camera's pairing QR; reauth = fresh QR.

Credentials come from the pairing-QR text (docs/vtech-lan-kit/docs/05-provisioning.md); the QR text itself is
never stored.
"""
from __future__ import annotations

import logging
import platform
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from . import native
from .const import (
    CONF_ACCOUNT,
    CONF_AUTHKEY,
    CONF_AV_PASSWORD,
    CONF_MAC,
    CONF_QR,
    CONF_UID,
    DOMAIN,
    TESTED_MODELS,
    TESTED_PRODUCT,
)
from .manager import SetupError, async_load, async_probe
from .vtech_lan import provision
from .vtech_lan.errors import AuthFailed, Unreachable, VtechError
from .vtech_lan.worker_client import WorkerClient

_LOG = logging.getLogger(__name__)

QR_SCHEMA = vol.Schema({vol.Required(CONF_QR): TextSelector(TextSelectorConfig(multiline=True))})


class VtechBabyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._client: WorkerClient | None = None
        self._model: str | None = None                    # from the connection test
        self._pending: tuple[str, dict[str, str]] | None = None   # entry awaiting the model warning

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if await self.hass.async_add_executor_job(native.host_supported) is None:
            return self.async_abort(reason="unsupported_host",
                                    description_placeholders={"machine": platform.machine() or "unknown"})
        return await self.async_step_camera()

    # One camera, from its pairing QR
    async def async_step_camera(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            creds, errors = self._decode(user_input[CONF_QR])
            if creds is not None:
                await self.async_set_unique_id(creds.uid)
                self._abort_if_unique_id_configured()      # rotated credentials: use reauth
                errors = await self._async_test(creds)
            if not errors:
                self._pending = (creds.name or f"VTech {creds.uid[-4:]}",
                                 _entry_creds(creds))
                if (self._model or "").strip().upper() not in TESTED_MODELS:
                    return await self.async_step_untested_model()
                return self.async_create_entry(title=self._pending[0], data=self._pending[1])
        return self.async_show_form(step_id="camera", data_schema=QR_SCHEMA, errors=errors)

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

    # Reauth: a fresh QR for the same camera
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            creds, errors = self._decode(user_input[CONF_QR], expect_uid=entry.unique_id)
            if creds is not None:
                errors = await self._async_test(creds)
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=_entry_creds(creds))
        return self.async_show_form(step_id="reauth_confirm", data_schema=QR_SCHEMA, errors=errors,
                                    description_placeholders={"name": entry.title})

    @staticmethod
    def _decode(text: str, expect_uid: str | None = None
                ) -> tuple[provision.Credentials | None, dict[str, str]]:
        try:
            creds = provision.provision(text)
        except provision.InvalidQr as err:
            _LOG.debug("invalid pairing QR: %s", err)
            return None, {CONF_QR: "invalid_qr"}
        if expect_uid is not None and creds.uid != expect_uid:
            return None, {CONF_QR: "wrong_camera"}
        return creds, {}

    async def _async_test(self, creds: provision.Credentials) -> dict[str, str]:
        """Connect once with the decoded credentials. Returns form errors (empty = OK)."""
        if self._client is None:
            try:
                self._client = await async_load(self.hass)
            except SetupError as err:
                _LOG.warning("the protocol module is not usable: %s", err)
                return {"base": "module_unavailable"}
        try:
            dev = await async_probe(self.hass, self._client, _entry_creds(creds))
        except AuthFailed:
            return {"base": "invalid_auth"}
        except Unreachable:
            return {"base": "cannot_connect"}
        except VtechError:
            _LOG.exception("unexpected error testing the camera connection")
            return {"base": "unknown"}
        self._model = dev.model
        return {}


def _entry_creds(creds: provision.Credentials) -> dict[str, str]:
    return {CONF_UID: creds.uid, CONF_ACCOUNT: creds.account, CONF_AV_PASSWORD: creds.av_password,
            CONF_AUTHKEY: creds.authkey, CONF_MAC: creds.mac}
