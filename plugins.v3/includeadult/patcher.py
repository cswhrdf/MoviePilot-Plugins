"""Patch layer that appends ``include_adult`` to TMDB search requests.

The module is intentionally free of MoviePilot imports at load time: host symbols are
resolved lazily inside :meth:`SearchParamPatcher.install`, so the pure helpers can be
unit tested without the backend. Every function in this module is side effect free
except ``install``/``uninstall``, which replace and restore host attributes.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Hosts that may serve the TMDB v3 API; a mirror can be injected at runtime.
DEFAULT_DOMAINS: tuple[str, ...] = ("api.themoviedb.org", "api.tmdb.org")
# Only the search endpoints are rewritten; details, seasons and discovery are untouched.
SEARCH_PATH_PATTERN = re.compile(r"^/3/search/(?:movie|tv|multi)$")
# Query key appended by this plugin.
INCLUDE_ADULT_KEY = "include_adult"
# Marker stored on wrapper functions so ownership can be inspected from the outside.
INSTALL_MARKER = "__include_adult_patch__"

ParamsInput = str | dict[str, Any] | None
ParamsResult = tuple[ParamsInput, bool]

# One global patcher at a time: hot reload may build a new instance before the old one
# is stopped, so install always tears down the previous owner first.
_GLOBAL_LOCK = threading.RLock()
_ACTIVE_PATCHER: SearchParamPatcher | None = None


@dataclass(frozen=True)
class InstallResult:
    """Outcome of one ``install`` attempt.

    Attributes:
        status: ``skipped``, ``installed``, ``partial`` or ``unsupported``.
        targets: Labels of the wrapped entry points, for logging and plugin data.
        issues: Human readable failures recorded while installing.

    """

    status: str
    targets: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()

    @property
    def installed(self) -> bool:
        """Return True when at least one transport layer is wrapped."""
        return self.status in {"installed", "partial"}


@dataclass
class _PatchRecord:
    """One replaced host attribute, kept for an exact restore."""

    owner: Any
    attribute: str
    original: Any
    wrapper: Any
    descriptor: str
    label: str


def is_tmdb_search_url(url: str, domains: Sequence[str] = DEFAULT_DOMAINS) -> bool:
    """Return True when ``url`` is an HTTPS TMDB search request.

    Args:
        url: Full request URL, with or without a query string.
        domains: Accepted host names; hosts are compared case insensitively.

    Returns:
        True only for ``https`` URLs whose path is ``/3/search/{movie|tv|multi}``
        and whose host is listed in ``domains``.

    """
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() != "https":
        return False
    host = (parts.hostname or "").lower()
    if host not in {str(domain).lower() for domain in domains}:
        return False
    return bool(SEARCH_PATH_PATTERN.match(parts.path))


def rewrite_query(query: str, include_adult: bool, force: bool) -> tuple[str, bool]:
    """Append or replace ``include_adult`` in a raw query string.

    Args:
        query: Raw query string without the leading ``?``.
        include_adult: Value written when the key is added or replaced.
        force: When False an existing key keeps its original value.

    Returns:
        ``(new_query, changed)``. An existing key with ``force=False`` returns the
        original string unchanged so callers never accumulate duplicates.

    """
    value = "true" if include_adult else "false"
    pairs = parse_qsl(query, keep_blank_values=True)
    for index, (key, _old_value) in enumerate(pairs):
        if key != INCLUDE_ADULT_KEY:
            continue
        if not force:
            return query, False
        pairs[index] = (key, value)
        return urlencode(pairs), True
    pairs.append((INCLUDE_ADULT_KEY, value))
    return urlencode(pairs), True


def rewrite_params(
    url: str,
    params: ParamsInput,
    include_adult: bool,
    force: bool,
    domains: Sequence[str] = DEFAULT_DOMAINS,
) -> ParamsResult:
    """Rewrite request ``params`` for a matching TMDB search URL.

    The URL's own query string is merged into the returned params first, so a caller
    that applies the result must send the URL without its query string (see
    :func:`rewrite_request`). Ordering follows ``url_query`` followed by ``params``,
    matching how ``requests`` and ``httpx`` would have combined them.

    Args:
        url: Full request URL used to decide whether the request is in scope.
        params: ``None``, a raw query string, or a mapping of query parameters.
        include_adult: Value written when the key is added or replaced.
        force: When False an existing key keeps its original value.
        domains: Accepted TMDB host names.

    Returns:
        ``(params, changed)``. Unmatched URLs and untouched requests return the
        original object untouched with ``changed=False``. ``None`` input becomes a
        query string when the URL matches and the request is rewritten.

    """
    if not is_tmdb_search_url(url, domains):
        return params, False
    try:
        url_query = urlsplit(url).query
    except ValueError:
        return params, False

    if params is None:
        new_query, changed = rewrite_query(url_query, include_adult, force)
        return (new_query, True) if changed else (None, False)

    if isinstance(params, str):
        combined = _join_query(url_query, params)
        new_query, changed = rewrite_query(combined, include_adult, force)
        return (new_query, True) if changed else (params, False)

    if isinstance(params, Mapping):
        pairs: list[tuple[str, str]] = list(parse_qsl(url_query, keep_blank_values=True))
        pairs.extend((str(key), _stringify(value)) for key, value in params.items())
        new_query, changed = rewrite_query(urlencode(pairs), include_adult, force)
        if not changed:
            return params, False
        return dict(parse_qsl(new_query, keep_blank_values=True)), True

    return params, False


def rewrite_url(
    url: str,
    include_adult: bool,
    force: bool,
    domains: Sequence[str] = DEFAULT_DOMAINS,
) -> tuple[str, bool]:
    """Append ``include_adult`` directly to a URL query string.

    This is the prepared-request entry point: by the time ``requests.Session.send``
    is called the params are already encoded into the URL.

    Args:
        url: Full request URL.
        include_adult: Value written when the key is added or replaced.
        force: When False an existing key keeps its original value.
        domains: Accepted TMDB host names.

    Returns:
        ``(new_url, changed)`` with the original URL returned when nothing is rewritten.

    """
    if not is_tmdb_search_url(url, domains):
        return url, False
    try:
        parts = urlsplit(url)
    except ValueError:
        return url, False
    new_query, changed = rewrite_query(parts.query, include_adult, force)
    if not changed:
        return url, False
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)), True


def rewrite_request(
    url: str,
    params: ParamsInput,
    include_adult: bool,
    force: bool,
    domains: Sequence[str] = DEFAULT_DOMAINS,
) -> tuple[str, ParamsInput, bool]:
    """Rewrite both URL and params of one request, ready to be sent.

    Args:
        url: Full request URL.
        params: ``None``, a raw query string, or a mapping of query parameters.
        include_adult: Value written when the key is added or replaced.
        force: When False an existing key keeps its original value.
        domains: Accepted TMDB host names.

    Returns:
        ``(url, params, changed)``. When rewritten, the URL query is removed because
        it has been folded into ``params``; otherwise both inputs are returned as is.

    """
    new_params, changed = rewrite_params(url, params, include_adult, force, domains)
    if not changed:
        return url, params, False
    parts = urlsplit(url)
    base_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))
    return base_url, new_params, True


def _join_query(first: str, second: str) -> str:
    """Join two raw query strings with a single ampersand, skipping empty parts."""
    if not first:
        return second
    if not second:
        return first
    return f"{first}&{second}"


def _stringify(value: Any) -> str:
    """Convert a mapping value to the string form used in a query string."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


class SearchParamPatcher:
    """Append include_adult to TMDB search requests right before they are sent."""

    def __init__(
        self,
        enabled: bool = True,
        include_adult: bool = True,
        force: bool = False,
        log_hits: bool = False,
        domains: Sequence[str] = DEFAULT_DOMAINS,
        logger: Any = None,
        time_source: Callable[[], float] = time.monotonic,
        name: str = "include_adult",
    ) -> None:
        """Build a patcher; nothing is installed until :meth:`install` is called.

        Args:
            enabled: When False ``install`` returns ``skipped`` and touches nothing.
            include_adult: Value written into the query string.
            force: When False an existing ``include_adult`` key is respected.
            log_hits: Emit one debug log per second at most while rewriting.
            domains: Accepted TMDB host names.
            logger: Optional object exposing ``debug``/``warning`` methods.
            time_source: Monotonic clock used for hit-log throttling.
            name: Plugin name used in log messages.

        """
        self.enabled = enabled
        self.include_adult = include_adult
        self.force = force
        self.log_hits = log_hits
        self.domains = tuple(domains)
        self.name = name
        self.hits = 0
        self.last_error = ""
        self.targets: tuple[str, ...] = ()
        self._logger = logger
        self._time_source = time_source
        self._log_interval = 1.0
        self._last_log_at = float("-inf")
        self._records: list[_PatchRecord] = []
        self._hits_lock = threading.Lock()
        self._warn_lock = threading.Lock()
        self._warned: set[str] = set()

    @property
    def originals(self) -> tuple[tuple[Any, str, Any], ...]:
        """Return ``(owner, attribute, original)`` records for the installed patches."""
        return tuple((record.owner, record.attribute, record.original) for record in self._records)

    @property
    def installed(self) -> bool:
        """Return True while at least one wrapper is installed by this instance."""
        return bool(self._records)

    def install(self) -> InstallResult:
        """Install the sync and async wrappers, replacing any previous installation.

        Sync side: ``RequestUtils.request`` first, then ``requests.Session.send``.
        Async side: ``AsyncRequestUtils._make_request`` first, then ``httpx2`` or
        ``httpx`` ``AsyncClient.request``. A failing side is reported as an issue but
        the other side stays installed.

        Returns:
            The installation result; ``status`` is ``skipped`` when disabled.

        """
        global _ACTIVE_PATCHER
        with _GLOBAL_LOCK:
            if _ACTIVE_PATCHER is not None and _ACTIVE_PATCHER is not self:
                _ACTIVE_PATCHER.uninstall()
            self.uninstall()
            if not self.enabled:
                return InstallResult("skipped")
            issues: list[str] = []
            sync_target = self._install_sync(issues)
            async_target = self._install_async(issues)
            if self._records:
                _ACTIVE_PATCHER = self
            targets = tuple(target for target in (sync_target, async_target) if target)
            self.targets = targets
            self.last_error = "; ".join(issues)
            if not targets:
                return InstallResult("unsupported", (), tuple(issues))
            status = "installed" if len(targets) == 2 else "partial"
            return InstallResult(status, targets, tuple(issues))

    def uninstall(self) -> None:
        """Restore every attribute installed by this instance, newest first.

        Attributes replaced by someone else after installation are left untouched and
        reported as a warning, so third-party patches are never overwritten.
        """
        global _ACTIVE_PATCHER
        with _GLOBAL_LOCK:
            for record in reversed(self._records):
                if not self._owned_by_us(record):
                    self._warn(f"[{self.name}] skip restoring {record.label}: attribute replaced by another patch")
                    continue
                self._restore(record)
            self._records.clear()
            self.targets = ()
            if _ACTIVE_PATCHER is self:
                _ACTIVE_PATCHER = None

    def _install_sync(self, issues: list[str]) -> str | None:
        """Wrap the highest priority sync entry point that exists."""
        original = _resolve_symbol("app.sdk.network", "RequestUtils", "request", issues)
        if original is not None:
            return self._install_request_like(original, _lookup_symbol("app.sdk.network", "RequestUtils"), "request")
        original = _resolve_symbol("requests.sessions", "Session", "send", issues)
        if original is not None:
            return self._install_send(original, _lookup_symbol("requests.sessions", "Session"))
        issues.append("sync transport not found: RequestUtils.request and Session.send are both unavailable")
        return None

    def _install_async(self, issues: list[str]) -> str | None:
        """Wrap the highest priority async entry point that exists."""
        owner = _lookup_symbol("app.sdk.network", "AsyncRequestUtils")
        original = _resolve_symbol("app.sdk.network", "AsyncRequestUtils", "_make_request", issues)
        if original is not None:
            if not inspect.iscoroutinefunction(original):
                issues.append(
                    "async transport not supported: AsyncRequestUtils._make_request is not a coroutine function"
                )
                original = None
            else:
                return self._install_request_like(original, owner, "_make_request", is_async=True)

        httpx_owner = _lookup_symbol("httpx2", "AsyncClient") or _lookup_symbol("httpx", "AsyncClient")
        if httpx_owner is None:
            issues.append("async transport not found: neither httpx2 nor httpx exposes AsyncClient")
            return None
        httpx_original = getattr(httpx_owner, "request", None)
        if httpx_original is None:
            issues.append("async transport not found: AsyncClient.request is unavailable")
            return None
        if not inspect.iscoroutinefunction(httpx_original):
            issues.append("async transport not supported: AsyncClient.request is not a coroutine function")
            return None
        return self._install_request_like(httpx_original, httpx_owner, "request", is_async=True)

    def _install_request_like(self, original: Any, owner: Any, attribute: str, is_async: bool = False) -> str | None:
        """Install a wrapper around a ``url``/``params`` style entry point."""
        if owner is None:
            return None

        wrapper = self._build_async_wrapper(original) if is_async else self._build_sync_wrapper(original)
        setattr(wrapper, INSTALL_MARKER, True)
        label = f"{_owner_name(owner)}.{attribute}"
        return label if self._replace(owner, attribute, original, wrapper, label) else None

    def _build_sync_wrapper(self, original: Any) -> Any:
        """Build a sync wrapper that rewrites the call in place."""

        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            call_args, call_kwargs = self._rewrite_call(original, args, kwargs)
            return original(*call_args, **call_kwargs)

        return wrapper

    def _build_async_wrapper(self, original: Any) -> Any:
        """Build an async wrapper that awaits the rewritten call."""

        @functools.wraps(original)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            call_args, call_kwargs = self._rewrite_call(original, args, kwargs)
            return await original(*call_args, **call_kwargs)

        return wrapper

    def _install_send(self, original: Any, owner: Any) -> str | None:
        """Install a wrapper around ``requests.Session.send``."""
        if owner is None:
            return None

        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            call_args, call_kwargs = self._rewrite_send_call(original, args, kwargs)
            return original(*call_args, **call_kwargs)

        setattr(wrapper, INSTALL_MARKER, True)
        label = "requests.sessions.Session.send"
        return (
            label if self._replace(owner, attribute="send", original=original, wrapper=wrapper, label=label) else None
        )

    def _replace(self, owner: Any, attribute: str, original: Any, wrapper: Any, label: str) -> bool:
        """Swap the attribute in place while preserving its descriptor kind."""
        try:
            descriptor = inspect.getattr_static(owner, attribute)
        except AttributeError:
            self._warn(f"[{self.name}] cannot install {label}: attribute disappeared")
            return False
        kind = "plain"
        if isinstance(descriptor, staticmethod):
            kind = "static"
            replacement: Any = staticmethod(wrapper)
        elif isinstance(descriptor, classmethod):
            kind = "class"
            replacement = classmethod(wrapper)
        else:
            replacement = wrapper
        try:
            setattr(owner, attribute, replacement)
        except (AttributeError, TypeError) as exc:
            self._warn(f"[{self.name}] cannot install {label}: {exc}")
            return False
        self._records.append(
            _PatchRecord(
                owner=owner,
                attribute=attribute,
                original=original,
                wrapper=wrapper,
                descriptor=kind,
                label=label,
            )
        )
        self._debug(f"[{self.name}] installed patch on {label}")
        return True

    def _restore(self, record: _PatchRecord) -> None:
        """Put the original attribute back, keeping its descriptor kind."""
        if record.descriptor == "static":
            replacement: Any = staticmethod(record.original)
        elif record.descriptor == "class":
            replacement = classmethod(record.original)
        else:
            replacement = record.original
        try:
            setattr(record.owner, record.attribute, replacement)
        except (AttributeError, TypeError) as exc:
            self._warn(f"[{self.name}] cannot restore {record.label}: {exc}")
            return
        self._debug(f"[{self.name}] restored {record.label}")

    def _owned_by_us(self, record: _PatchRecord) -> bool:
        """Return True when the current attribute is exactly the wrapper we installed."""
        current = getattr(record.owner, record.attribute, None)
        if record.descriptor == "class":
            return getattr(current, "__func__", None) is record.wrapper
        return current is record.wrapper

    def _bind_call(
        self,
        original: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> tuple[inspect.Signature, inspect.BoundArguments] | None:
        """Bind a call to the wrapped signature, warning once when the shape is unknown.

        A signature that cannot be inspected or a call that does not bind means the
        request cannot be rewritten safely; the call is passed through untouched.

        Returns:
            ``(signature, bound)``, or ``None`` when the call must pass through as is.

        """
        label = getattr(original, "__qualname__", None) or getattr(original, "__name__", "callable")
        try:
            signature = inspect.signature(original)
        except (TypeError, ValueError) as exc:
            self._warn_once(f"signature:{label}", f"[{self.name}] cannot inspect {label}: {exc}; passing through")
            return None
        try:
            return signature, signature.bind(*args, **kwargs)
        except TypeError as exc:
            self._warn_once(f"bind:{label}", f"[{self.name}] cannot bind call to {label}: {exc}; passing through")
            return None

    def _rewrite_call(
        self,
        original: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Rewrite a ``(method, url, ..., params=...)`` style call without changing shape."""
        bound_pair = self._bind_call(original, args, kwargs)
        if bound_pair is None:
            return args, kwargs
        signature, bound = bound_pair
        arguments = bound.arguments
        url = arguments.get("url")
        if not isinstance(url, str):
            return args, kwargs
        container, params_key = _locate_params(signature, arguments)
        if container is None:
            return args, kwargs
        params = container[params_key] if params_key in container else _param_default(signature, params_key)
        try:
            new_url, new_params, changed = rewrite_request(
                url,
                params,
                self.include_adult,
                self.force,
                self.domains,
            )
        except Exception as exc:  # noqa: BLE001 - never break the outgoing request
            self._warn(f"[{self.name}] rewrite skipped for {url}: {exc}")
            return args, kwargs
        if not changed:
            return args, kwargs
        arguments["url"] = new_url
        container[params_key] = new_params
        self._record_hit()
        return bound.args, bound.kwargs

    def _rewrite_send_call(
        self,
        original: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Rewrite a prepared request URL right before ``Session.send``."""
        bound_pair = self._bind_call(original, args, kwargs)
        if bound_pair is None:
            return args, kwargs
        _signature, bound = bound_pair
        request = bound.arguments.get("request")
        if request is None:
            return args, kwargs
        url = getattr(request, "url", None)
        if not isinstance(url, str):
            return args, kwargs
        try:
            new_url, changed = rewrite_url(url, self.include_adult, self.force, self.domains)
        except Exception as exc:  # noqa: BLE001 - never break the outgoing request
            self._warn(f"[{self.name}] rewrite skipped for {url}: {exc}")
            return args, kwargs
        if not changed:
            return args, kwargs
        try:
            request.url = new_url
        except (AttributeError, TypeError) as exc:
            self._warn(f"[{self.name}] cannot rewrite prepared request: {exc}")
            return args, kwargs
        self._record_hit()
        return bound.args, bound.kwargs

    def _record_hit(self) -> None:
        """Count one rewritten request and emit a throttled debug line."""
        with self._hits_lock:
            self.hits += 1
            hits = self.hits
            now = self._time_source()
            should_log = self.log_hits and (now - self._last_log_at) >= self._log_interval
            if should_log:
                self._last_log_at = now
        if should_log and self._logger is not None:
            self._logger.debug(f"[{self.name}] include_adult injected: hits={hits}")

    def _warn(self, message: str) -> None:
        """Record and log a non fatal patch problem."""
        self.last_error = message
        if self._logger is not None:
            self._logger.warning(message)

    def _warn_once(self, key: str, message: str) -> None:
        """Warn about a recurring condition only once per ``key`` to avoid log flooding."""
        with self._warn_lock:
            if key in self._warned:
                return
            self._warned.add(key)
        self._warn(message)

    def _debug(self, message: str) -> None:
        """Log an installation detail when a logger was provided."""
        if self._logger is not None:
            self._logger.debug(message)


def _locate_params(
    signature: inspect.Signature,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Find the container that holds ``params`` in a bound call.

    Returns:
        ``(container, key)`` where ``container[key]`` is the params value; ``container``
        is ``None`` when the signature accepts neither ``params`` nor ``**kwargs``.

    """
    if "params" in signature.parameters:
        return arguments, "params"
    var_keyword = next(
        (name for name, parameter in signature.parameters.items() if parameter.kind is parameter.VAR_KEYWORD),
        None,
    )
    if var_keyword is not None:
        extra = arguments.get(var_keyword)
        if not isinstance(extra, dict):
            extra = {}
            arguments[var_keyword] = extra
        return extra, "params"
    return None, "params"


def _param_default(signature: inspect.Signature, name: str) -> Any:
    """Return the declared default of ``name`` or ``None`` when it has none."""
    parameter = signature.parameters.get(name)
    if parameter is None or parameter.default is inspect.Parameter.empty:
        return None
    return parameter.default


def _lookup_symbol(module_name: str, attribute: str) -> Any:
    """Import ``module_name`` and return ``attribute``; ``None`` when unavailable."""
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 - host module may be absent in tests
        return None
    return getattr(module, attribute, None)


def _resolve_symbol(module_name: str, attribute: str, nested: str, issues: list[str]) -> Any:
    """Resolve ``module.attribute.nested`` returning the raw callable, or ``None``."""
    owner = _lookup_symbol(module_name, attribute)
    if owner is None:
        issues.append(f"{module_name}.{attribute} is unavailable")
        return None
    try:
        descriptor = inspect.getattr_static(owner, nested)
    except AttributeError:
        issues.append(f"{attribute}.{nested} is unavailable")
        return None
    if isinstance(descriptor, (staticmethod, classmethod)):
        return descriptor.__func__
    return descriptor


def _owner_name(owner: Any) -> str:
    """Return ``module.ClassName`` for logging."""
    return f"{getattr(owner, '__module__', '?')}.{getattr(owner, '__qualname__', getattr(owner, '__name__', '?'))}"
