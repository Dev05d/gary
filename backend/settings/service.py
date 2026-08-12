"""Runtime settings: database overrides layered on top of `.env`.

Three layers, lowest to highest precedence:

    1. field defaults in `backend/config.py`
    2. environment / `.env`
    3. overrides saved from the settings UI (the `app_settings` table)

The effective `Settings` object is rebuilt from all three whenever something
changes, and the LLM registry is rebuilt with it — so switching model or
inference host takes effect on the next message, without a restart. Settings
marked `restart=True` in the catalog genuinely cannot be applied live (the
socket is already bound) and the UI says so.

`.env` is never written to. It stays the operator's file; the UI's changes live
in the database, and the API reports which layer each value came from so the
provenance is always visible.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.database.models import AppSetting
from backend.settings.catalog import (
    BY_KEY,
    CATALOG,
    SettingDef,
    SettingValidationError,
    coerce,
    validate,
)

log = logging.getLogger(__name__)

OVERRIDE_PREFIX = "config:"

Source = str  # "default" | "env" | "database"


class SettingsService:
    """Reads and writes the database override layer."""

    def __init__(self, env_settings: Settings) -> None:
        # The env+defaults baseline, captured once at boot.
        self._env_settings = env_settings

    # ------------------------------------------------------------- reading
    async def overrides(self, session: AsyncSession) -> Dict[str, Any]:
        rows = (
            await session.execute(
                select(AppSetting).where(AppSetting.key.startswith(OVERRIDE_PREFIX))
            )
        ).scalars().all()
        out: Dict[str, Any] = {}
        for row in rows:
            key = row.key[len(OVERRIDE_PREFIX) :]
            if key not in BY_KEY:
                continue  # setting was removed from the catalog; ignore stale row
            value = (row.value or {}).get("v")
            if value is not None:
                out[key] = value
        return out

    async def effective(self, session: AsyncSession) -> Settings:
        """Build the Settings the app should actually run with."""
        overrides = await self.overrides(session)
        return self.merge(overrides)

    def merge(self, overrides: Dict[str, Any]) -> Settings:
        base = self._env_settings.model_dump()
        base.update(overrides)
        # Re-validate through pydantic so cross-field rules still apply — most
        # importantly the refusal to bind a non-loopback host without a token.
        return Settings(**base)

    def env_value(self, key: str) -> Any:
        return getattr(self._env_settings, key, None)

    def default_value(self, key: str) -> Any:
        field = Settings.model_fields.get(key)
        if field is None:
            return None
        default = field.default
        if default is not None and hasattr(default, "__call__"):
            return None
        return default

    def source_of(self, key: str, overrides: Dict[str, Any]) -> Source:
        if key in overrides:
            return "database"
        env = self.env_value(key)
        default = self.default_value(key)
        if env != default:
            return "env"
        return "default"

    # ------------------------------------------------------------- writing
    async def apply(
        self, session: AsyncSession, changes: Dict[str, Any]
    ) -> Tuple[Settings, List[str], List[str]]:
        """Validate and persist a batch of changes.

        Returns (new effective settings, changed keys, keys needing a restart).
        Raises SettingValidationError before writing anything, so a bad value in
        the batch cannot leave settings half-applied.
        """
        unknown = [k for k in changes if k not in BY_KEY]
        if unknown:
            raise SettingValidationError(unknown[0], f"Unknown setting: {unknown[0]}")

        cleaned: Dict[str, Any] = {}
        for key, raw in changes.items():
            defn = BY_KEY[key]
            try:
                value = coerce(defn, raw)
            except (TypeError, ValueError):
                raise SettingValidationError(
                    key, f"{defn.label}: {raw!r} is not a valid {defn.type}."
                ) from None
            cleaned[key] = validate(defn, value)

        # Prove the merged result is constructible *before* persisting, so an
        # invalid combination (0.0.0.0 with no token) is rejected up front.
        current = await self.overrides(session)
        candidate = dict(current)
        for key, value in cleaned.items():
            if value is None:
                candidate.pop(key, None)
            else:
                candidate[key] = value
        try:
            new_settings = self.merge(candidate)
        except Exception as exc:  # pydantic ValidationError
            raise SettingValidationError("__all__", _first_error_message(exc)) from None

        changed: List[str] = []
        for key, value in cleaned.items():
            before = current.get(key, self.env_value(key))
            if value is None:
                if key in current:
                    await self._delete(session, key)
                    changed.append(key)
                continue
            if value != before or key not in current:
                await self._write(session, key, value)
                changed.append(key)

        await session.commit()

        restart_needed = sorted({k for k in changed if BY_KEY[k].restart})
        return new_settings, changed, restart_needed

    async def reset(self, session: AsyncSession, key: str) -> Settings:
        """Drop the database override, falling back to .env / default."""
        if key not in BY_KEY:
            raise SettingValidationError(key, f"Unknown setting: {key}")
        await self._delete(session, key)
        await session.commit()
        return await self.effective(session)

    async def reset_all(self, session: AsyncSession) -> Settings:
        for key in list(BY_KEY):
            await self._delete(session, key)
        await session.commit()
        return await self.effective(session)

    async def _write(self, session: AsyncSession, key: str, value: Any) -> None:
        row_key = OVERRIDE_PREFIX + key
        row = await session.get(AppSetting, row_key)
        if row is None:
            session.add(AppSetting(key=row_key, value={"v": value}))
        else:
            row.value = {"v": value}

    async def _delete(self, session: AsyncSession, key: str) -> None:
        row = await session.get(AppSetting, OVERRIDE_PREFIX + key)
        if row is not None:
            await session.delete(row)

    # ----------------------------------------------------------- describing
    async def describe(
        self, session: AsyncSession, settings: Settings
    ) -> List[Dict[str, Any]]:
        """Render the catalog with current values and provenance, for the UI."""
        overrides = await self.overrides(session)
        out: List[Dict[str, Any]] = []
        for defn in CATALOG:
            value = getattr(settings, defn.key, None)
            out.append(
                {
                    **_definition_payload(defn),
                    "value": _presentable(defn, value),
                    "is_set": bool(value) if defn.sensitive else None,
                    "source": self.source_of(defn.key, overrides),
                    "env_value": (
                        None if defn.sensitive else _presentable(defn, self.env_value(defn.key))
                    ),
                    "default_value": (
                        None
                        if defn.sensitive
                        else _presentable(defn, self.default_value(defn.key))
                    ),
                    "overridden": defn.key in overrides,
                }
            )
        return out


def _definition_payload(defn: SettingDef) -> Dict[str, Any]:
    return {
        "key": defn.key,
        "label": defn.label,
        "description": defn.description,
        "category": defn.category,
        "type": defn.type,
        "minimum": defn.minimum,
        "maximum": defn.maximum,
        "step": defn.step,
        "options": defn.options,
        "placeholder": defn.placeholder,
        "unit": defn.unit,
        "model_host_key": defn.model_host_key,
        "advanced": defn.advanced,
        "restart": defn.restart,
        "sensitive": defn.sensitive,
        "active": defn.active,
        "milestone": defn.milestone,
        "warning": defn.warning,
        "examples": defn.examples,
    }


def _presentable(defn: SettingDef, value: Any) -> Any:
    """Never leak a secret's value to the browser."""
    if defn.sensitive:
        return None
    if value is None:
        return None
    from pathlib import Path

    if isinstance(value, Path):
        return str(value)
    return value


def _first_error_message(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
            msg = str(first.get("msg", "")).replace("Value error, ", "")
            return msg or str(exc)
        except Exception:  # noqa: BLE001
            pass
    return str(exc)


_service: Optional[SettingsService] = None


def get_settings_service(env_settings: Optional[Settings] = None) -> SettingsService:
    global _service
    if _service is None:
        if env_settings is None:
            from backend.config import get_settings

            env_settings = get_settings()
        _service = SettingsService(env_settings)
    return _service


def set_settings_service(service: Optional[SettingsService]) -> None:
    global _service
    _service = service
