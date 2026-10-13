"""Config flow: the SDK (uploaded once per HA instance as the Kalay Mobile app), then the camera's
pairing QR; reauth = fresh QR.

The SDK libraries are extracted from the user's upload by
native.install_sdk (docs/vtech-lan-kit/docs/85-ha-deployment.md); the upload itself is deleted afterwards.
Credentials come from the pairing-QR text (docs/vtech-lan-kit/docs/05-provisioning.md); the QR text itself is
never stored.
"""
from __future__ import annotations

import logging
import platform
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    TextSelector,
    TextSelectorConfig,
)

from . import native
from .const import (
    CONF_ACCOUNT,
    CONF_APP_FILE,
    CONF_AUTHKEY,
    CONF_AV_PASSWORD,
    CONF_MAC,
    CONF_QR,
    CONF_SDK_DIR,
    CONF_UID,
    DOMAIN,
    KALAY_APP,
    SDK_SUBDIR,
    TESTED_MODELS,
    TESTED_PRODUCT,
)
from .manager import SdkHandle, SdkSetupError, async_load_sdk, async_probe
from .vtech_lan import provision
from .vtech_lan.errors import AuthFailed, Unlicensed, Unreachable, VtechError

_LOG = logging.getLogger(__name__)

QR_SCHEMA = vol.Schema({vol.Required(CONF_QR): TextSelector(TextSelectorConfig(multiline=True))})
APP_SCHEMA = vol.Schema({vol.Required(CONF_APP_FILE): FileSelector(FileSelectorConfig(accept=".xapk,.apk,.apks,.zip"))})


def _install(hass: HomeAssistant, file_id: str, sdk_dir: str) -> str:
    """Executor job: extract the SDK from the uploaded zip into sdk_dir. The upload is then deleted."""
    with process_uploaded_file(hass, file_id) as path:
        return native.install_sdk(str(path), sdk_dir)


class VtechBabyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._sdk_dir: str | None = None
        self._handle: SdkHandle | None = None
        self._model: str | None = None                    # from the connection test
        self._pending: tuple[str, dict[str, str]] | None = None   # entry awaiting the model warning

    # Step 1: the SDK, once per HA instance
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        arch = await self.hass.async_add_executor_job(native.host_supported)
        if arch is None:
            return self.async_abort(reason="unsupported_host",
                                    description_placeholders={"machine": platform.machine() or "unknown"})
        placeholders = {"app": KALAY_APP, "arch": arch, "abi": native.ABI_OF[arch],
                        "required": native.SDK_VERSION, "version": "", "found": "", "missing": "", "error": ""}

        if user_input is None:
            # Already here: an existing camera's SDK, or the app uploaded before on this host.
            existing = self._async_current_entries(include_ignore=False)
            self._sdk_dir = existing[0].data[CONF_SDK_DIR] if existing else self.hass.config.path(*SDK_SUBDIR)
            if existing or await self.hass.async_add_executor_job(native.sdk_present, self._sdk_dir):
                try:
                    self._handle = await async_load_sdk(self.hass, self._sdk_dir)
                except SdkSetupError as err:
                    _LOG.warning("the SDK in %s is not usable (%s); asking for the app again", self._sdk_dir, err)
                else:
                    return await self.async_step_camera()
            return self.async_show_form(step_id="user", data_schema=APP_SCHEMA, description_placeholders=placeholders)

        errors: dict[str, str] = {}
        self._sdk_dir = self.hass.config.path(*SDK_SUBDIR)
        try:
            await self.hass.async_add_executor_job(_install, self.hass, user_input[CONF_APP_FILE], self._sdk_dir)
        except native.ExtractError as err:
            errors["base"] = _install_error(err, placeholders)
        except (OSError, ValueError) as err:   # ValueError: the upload id is unknown or expired
            errors["base"], placeholders["error"] = "sdk_error", str(err)
        else:
            try:
                self._handle = await async_load_sdk(self.hass, self._sdk_dir)
            except SdkSetupError as err:
                errors["base"], placeholders["error"] = "sdk_error", str(err)
            else:
                return await self.async_step_camera()
        return self.async_show_form(step_id="user", data_schema=APP_SCHEMA, errors=errors,
                                    description_placeholders=placeholders)

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
                self._pending = (creds.name or f"VTech {creds.uid[-4:]}",
                                 {CONF_SDK_DIR: self._sdk_dir, **_entry_creds(creds)})
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
            dev = await async_probe(self.hass, self._handle.client, _entry_creds(creds))
        except AuthFailed:
            return {"base": "invalid_auth"}
        except Unreachable:
            return {"base": "cannot_connect"}
        except Unlicensed:
            return {"base": "invalid_license"}
        except VtechError:
            _LOG.exception("unexpected error testing the camera connection")
            return {"base": "unknown"}
        self._model = dev.model
        return {}


def _install_error(err: Exception, placeholders: dict[str, str]) -> str:
    """Form error key for a failed SDK install, filling the placeholders it needs."""
    if isinstance(err, native.NotAPackage):
        return "not_a_package"
    if isinstance(err, native.WrongArch):
        placeholders["found"] = ", ".join(err.found) or "none"
        return "wrong_arch"
    if isinstance(err, native.MissingLibs):
        placeholders["missing"] = ", ".join(err.missing)
        return "missing_libraries"
    if isinstance(err, native.WrongVersion):
        placeholders["version"] = err.version
        return "wrong_version"
    placeholders["error"] = str(err)
    return "sdk_error"


def _entry_creds(creds: provision.Credentials) -> dict[str, str]:
    return {CONF_UID: creds.uid, CONF_ACCOUNT: creds.account, CONF_AV_PASSWORD: creds.av_password,
            CONF_AUTHKEY: creds.authkey, CONF_MAC: creds.mac}
