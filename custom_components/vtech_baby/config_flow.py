"""Config flow: SDK directory (once per HA instance), then the camera's pairing QR; reauth = fresh QR.

Design: HA_INTEGRATION.md §6 Phase 2. Credentials come from the pairing-QR text
(vtech-lan-kit/docs/05-provisioning.md); the QR text itself is never stored.
"""
from __future__ import annotations

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
    CONF_QR,
    CONF_SDK_DIR,
    CONF_UID,
    DOMAIN,
    SDK_SUBDIR,
)
from .manager import SdkHandle, SdkSetupError, async_load_sdk, async_probe
from .vtech_lan import provision
from .vtech_lan.errors import AuthFailed, Unlicensed, Unreachable, VtechError

_LOG = logging.getLogger(__name__)

QR_SCHEMA = vol.Schema({vol.Required(CONF_QR): TextSelector(TextSelectorConfig(multiline=True))})


class VtechBabyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._sdk_dir: str | None = None
        self._handle: SdkHandle | None = None

    # Step 1: the SDK, once per HA instance
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        existing = self._async_current_entries(include_ignore=False)
        if existing and user_input is None:
            self._sdk_dir = existing[0].data[CONF_SDK_DIR]
            try:
                self._handle = await async_load_sdk(self.hass, self._sdk_dir)
            except SdkSetupError:
                self._handle = None          # fall through to the form so the user can fix the path
            else:
                return await self.async_step_camera()

        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        if user_input is not None:
            self._sdk_dir = user_input[CONF_SDK_DIR].strip()
            try:
                self._handle = await async_load_sdk(self.hass, self._sdk_dir)
            except SdkSetupError as err:
                errors["base"], placeholders["error"] = "sdk_error", str(err)
            else:
                return await self.async_step_camera()

        default = self._sdk_dir or self.hass.config.path(*SDK_SUBDIR)
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({vol.Required(CONF_SDK_DIR, default=default): str}),
            errors=errors, description_placeholders=placeholders)

    # Step 2: one camera, from its pairing QR
    async def async_step_camera(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            creds, errors = self._decode(user_input[CONF_QR])
            if creds is not None:
                await self.async_set_unique_id(creds.uid)
                self._abort_if_unique_id_configured()      # rotated credentials: use reauth
                errors = await self._async_test(creds)
            if not errors:
                return self.async_create_entry(
                    title=creds.name or f"VTech {creds.uid[-4:]}",
                    data={CONF_SDK_DIR: self._sdk_dir, **_entry_creds(creds)})
        return self.async_show_form(step_id="camera", data_schema=QR_SCHEMA, errors=errors)

    # Reauth: a fresh QR for the same camera
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._sdk_dir = entry_data[CONF_SDK_DIR]
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
        if self._handle is None:
            try:
                self._handle = await async_load_sdk(self.hass, self._sdk_dir)
            except SdkSetupError as err:
                _LOG.warning("SDK not usable: %s", err)
                return {"base": "sdk_unavailable"}
        try:
            await async_probe(self.hass, self._handle.sdk, _entry_creds(creds))
        except AuthFailed:
            return {"base": "invalid_auth"}
        except Unreachable:
            return {"base": "cannot_connect"}
        except Unlicensed:
            return {"base": "invalid_license"}
        except VtechError:
            _LOG.exception("unexpected error testing the camera connection")
            return {"base": "unknown"}
        return {}


def _entry_creds(creds: provision.Credentials) -> dict[str, str]:
    return {CONF_UID: creds.uid, CONF_ACCOUNT: creds.account, CONF_AV_PASSWORD: creds.av_password,
            CONF_AUTHKEY: creds.authkey, CONF_MAC: creds.mac}
