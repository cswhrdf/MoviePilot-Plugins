"""MoviePilot plugin that injects ``include_adult=true`` into TMDB search requests.

The plugin only owns a single switch. Everything else (the injected value, the
override policy, the scope and the hit logging) is a code constant, so the config
page stays minimal and the behaviour is auditable in source control.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .patcher import DEFAULT_DOMAINS, InstallResult, SearchParamPatcher

# Fixed behaviour, intentionally not exposed as configuration.
INCLUDE_ADULT = True
FORCE_OVERRIDE = False
SCOPE = "search_only"
LOG_HITS = False

# Plugin data key: observation only, there is no detail page for this plugin.
DATA_KEY = "patch_status"


def normalize_config(config: Any) -> tuple[dict[str, Any], list[str]]:
    """Normalize the plugin config to the single ``enabled`` switch.

    Args:
        config: Raw config from the host; ``None`` and non mappings fall back to
            ``{"enabled": False}``.

    Returns:
        ``(normalized, issues)``. ``normalized`` only contains ``enabled`` and is safe
        to serialize as JSON; unusable inputs are reported through ``issues`` without
        blocking the plugin.

    """
    issues: list[str] = []
    if config is None:
        return {"enabled": False}, issues
    if not isinstance(config, Mapping):
        issues.append(f"config must be an object, got {type(config).__name__}; enabled defaults to false")
        return {"enabled": False}, issues
    if "enabled" not in config:
        return {"enabled": False}, issues
    raw = config["enabled"]
    if isinstance(raw, bool):
        return {"enabled": raw}, issues
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text == "true":
            return {"enabled": True}, issues
        if text in {"false", ""}:
            return {"enabled": False}, issues
    issues.append(f"enabled must be a boolean, got {raw!r}; falling back to false")
    return {"enabled": False}, issues


def _tmdb_domains() -> tuple[str, ...]:
    """Return the accepted TMDB hosts, following the host mirror setting when set."""
    domains = set(DEFAULT_DOMAINS)
    try:
        configured = str(getattr(settings, "TMDB_API_DOMAIN", "") or "").strip()
    except Exception:  # noqa: BLE001 - host settings may be unavailable in tests
        configured = ""
    if configured:
        candidate = configured if "://" in configured else f"https://{configured}"
        try:
            host = urlsplit(candidate).hostname
        except ValueError:
            host = None
        if host:
            domains.add(host.lower())
    return tuple(sorted(domains))


class IncludeAdult(_PluginBase):
    """Append include_adult=true to TMDB search requests."""

    # --- plugin metadata (used by the host UI and the marketplace) ---
    plugin_name = "include_adult"
    plugin_desc = ""
    plugin_icon = "TheMovieDb_A.png"
    plugin_version = "0.1.0"
    plugin_author = "cswhrdf"
    plugin_config_prefix = "includeadult_"
    plugin_order = 61
    auth_level = 1

    # Runtime state; class level defaults keep instances usable before init_plugin.
    _enabled: bool = False
    _config: dict[str, Any] | None = None
    _config_issues: tuple[str, ...] = ()
    _patcher: SearchParamPatcher | None = None
    # Hits of the session that just ended; the disable path rebuilds an idle patcher
    # whose zero counter must not overwrite the real count persisted on stop.
    _last_hits: int = 0

    def init_plugin(self, config: dict[str, Any] | None = None) -> None:
        """Reset runtime state, normalize the config and (re)install the patch.

        Args:
            config: Plugin config from the host; only ``enabled`` is consumed.

        """
        self.stop_service()

        normalized, issues = normalize_config(config)
        self._config = normalized
        self._config_issues = tuple(issues)
        self._enabled = bool(normalized["enabled"])
        if self._enabled:
            # A newly enabled session always counts from zero, never from the previous run.
            self._last_hits = 0
        for issue in issues:
            logger.warning(f"[{self.plugin_name}] {issue}")

        patcher = SearchParamPatcher(
            enabled=self._enabled,
            include_adult=INCLUDE_ADULT,
            force=FORCE_OVERRIDE,
            log_hits=LOG_HITS,
            domains=_tmdb_domains(),
            logger=logger,
            name=self.plugin_name,
        )
        self._patcher = patcher
        result = patcher.install()
        self._save_status(result)
        logger.info(
            f"[{self.plugin_name}] initialized: enabled={self._enabled}, "
            f"status={result.status}, targets={list(result.targets)}"
        )

    def get_state(self) -> bool:
        """Return whether the plugin switch is currently on.

        Returns:
            True when the patch is supposed to be active.

        """
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """Return the remote command list; this plugin registers none."""
        return []

    def get_api(self) -> list[dict[str, Any]]:
        """Return the plugin API list; this plugin registers none.

        Returns:
            An empty list.

        """
        return []

    def get_form(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return the config form with the single ``enabled`` switch.

        Returns:
            ``(form, default_model)``; the model only contains ``enabled``.

        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "Enable include_adult=true for TMDB search",
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ], {"enabled": False}

    def get_page(self) -> list[dict[str, Any]] | None:
        """Return nothing because this plugin has no detail page.

        The host treats a method body made only of placeholder statements as "not
        implemented"; keep the docstring + pass form so no "view data" entry appears.
        """
        pass

    def get_module(self) -> dict[str, Any] | None:
        """Return the module declaration; this plugin overrides no host module."""
        pass

    def stop_service(self) -> None:
        """Remove the patch and reset runtime state; safe to call repeatedly."""
        patcher = self._patcher
        if patcher is not None:
            # Capture the final count before the patch is torn down: the host runs
            # stop_service followed by init_plugin, which builds a zero-hit patcher.
            # An idle patcher (rebuild while disabled) must not zero the stored value.
            if patcher.hits > 0:
                self._last_hits = patcher.hits
            patcher.uninstall()
        self._enabled = False
        self._config = None
        self._config_issues = ()
        if patcher is not None:
            self._save_status(InstallResult("skipped", (), ()))
        self._patcher = None

    def _save_status(self, result: InstallResult) -> None:
        """Persist the patch state for diagnostics; failures never block the plugin."""
        patcher = self._patcher
        hits = patcher.hits if patcher is not None else 0
        if hits == 0 and result.status == "skipped":
            # The disabled state is reported by a freshly built patcher that never
            # sees traffic; keep the count of the session that just ended. A host
            # reload builds a new instance, so fall back to the persisted value.
            hits = self._last_hits if self._last_hits > 0 else self._stored_hits()
        payload = {
            "enabled": self._enabled,
            "status": result.status,
            "hits": hits,
            "patched_at": datetime.now(UTC).isoformat(),
            "targets": list(result.targets),
            "last_error": "; ".join(result.issues) or (patcher.last_error if patcher is not None else ""),
        }
        try:
            self.save_data(DATA_KEY, payload)
        except Exception as exc:  # noqa: BLE001 - diagnostics must never break the plugin
            logger.warning(f"[{self.plugin_name}] failed to save patch status: {exc}")

    @staticmethod
    def _parse_hits(data: Any) -> int:
        """Return the hit count of a stored payload, 0 when it is missing or invalid."""
        if not isinstance(data, Mapping):
            return 0
        value = data.get("hits")
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return value

    def _stored_hits(self) -> int:
        """Read the hit count persisted by a previous session; 0 when unavailable."""
        try:
            data = self.get_data(DATA_KEY)
        except Exception as exc:  # noqa: BLE001 - diagnostics must never break the plugin
            logger.debug(f"[{self.plugin_name}] cannot read previous patch status: {exc}")
            return 0
        return self._parse_hits(data)
