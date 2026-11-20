"""Contract tests for the pure patch helpers of the include_adult plugin.

Only the side effect free helpers and the disabled install path are covered here:
they answer whether a URL is in scope, how a query string or params mapping is
rewritten, and how encoded characters survive the rewrite. Wrapper installation on
host symbols is covered by ``test_plugin.py`` so this module stays independent from
the backend runtime.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

PLUGIN_ID = "includeadult"


def _load_patcher() -> Any:
    """Import the plugin patcher module through the production namespace."""
    return importlib.import_module(f"app.plugins.{PLUGIN_ID}.patcher")


@pytest.fixture()
def patcher() -> Any:
    """Return the plugin patcher module."""
    return _load_patcher()


# ---------------- URL scope ----------------


def test_search_urls_are_matched(patcher):
    """Search endpoints on accepted hosts match with and without a query string."""
    assert patcher.is_tmdb_search_url("https://api.themoviedb.org/3/search/movie?query=avatar&page=1") is True
    assert patcher.is_tmdb_search_url("https://api.themoviedb.org/3/search/tv") is True
    assert patcher.is_tmdb_search_url("https://api.themoviedb.org/3/search/multi?query=a") is True
    assert patcher.is_tmdb_search_url("https://API.THEMOVIEDB.ORG/3/search/movie") is True
    assert patcher.is_tmdb_search_url("https://api.tmdb.org/3/search/movie?query=a") is True


def test_runtime_domain_override_is_accepted(patcher):
    """A mirrored host passed at runtime is treated as a TMDB host."""
    assert (
        patcher.is_tmdb_search_url("https://mirror.example.com/3/search/movie", domains=("mirror.example.com",)) is True
    )


def test_out_of_scope_urls_are_rejected(patcher):
    """Details, non-https, unknown hosts and other search kinds are never rewritten."""
    assert patcher.is_tmdb_search_url("https://api.themoviedb.org/3/movie/550") is False
    assert patcher.is_tmdb_search_url("https://api.themoviedb.org/3/search/person?query=a") is False
    assert patcher.is_tmdb_search_url("https://api.themoviedb.org/3/discover/movie") is False
    assert patcher.is_tmdb_search_url("https://api.themoviedb.org/3/search/movie/") is False
    assert patcher.is_tmdb_search_url("http://api.themoviedb.org/3/search/movie") is False
    assert patcher.is_tmdb_search_url("https://example.com/3/search/movie") is False
    assert patcher.is_tmdb_search_url("") is False


# ---------------- query string rewrite ----------------


def test_missing_key_is_appended(patcher):
    """An absent include_adult key is appended without touching the other parameters."""
    new_query, changed = patcher.rewrite_query("query=avatar&page=1", include_adult=True, force=False)

    assert changed is True
    assert new_query == "query=avatar&page=1&include_adult=true"


def test_existing_key_is_kept_when_not_forced(patcher):
    """An existing key keeps its upstream value when force is off."""
    original = "query=avatar&include_adult=false"

    new_query, changed = patcher.rewrite_query(original, include_adult=True, force=False)

    assert changed is False
    assert new_query == original


def test_existing_key_is_replaced_when_forced(patcher):
    """A forced rewrite replaces the value in place and never duplicates the key."""
    new_query, changed = patcher.rewrite_query(
        "query=avatar&include_adult=false&page=2", include_adult=True, force=True
    )

    assert changed is True
    assert new_query.count("include_adult=") == 1
    assert "include_adult=true" in new_query
    assert "include_adult=false" not in new_query


def test_false_value_is_written_when_requested(patcher):
    """The injected value follows the include_adult flag."""
    new_query, changed = patcher.rewrite_query("query=avatar", include_adult=False, force=False)

    assert changed is True
    assert new_query == "query=avatar&include_adult=false"


# ---------------- params rewrite ----------------


def test_none_string_and_mapping_params(patcher):
    """None, string and mapping params are all supported for a matching URL."""
    url = "https://api.themoviedb.org/3/search/movie?api_key=k&language=zh-CN"

    none_params, changed = patcher.rewrite_params(url, None, include_adult=True, force=False)
    assert changed is True
    assert isinstance(none_params, str)
    assert "api_key=k" in none_params
    assert "include_adult=true" in none_params

    text_params, changed = patcher.rewrite_params(url, "query=avatar&page=1", include_adult=True, force=False)
    assert changed is True
    assert text_params.startswith("api_key=k&language=zh-CN&query=avatar&page=1&")
    assert "include_adult=true" in text_params

    mapping_params, changed = patcher.rewrite_params(
        url, {"query": "avatar", "page": 1}, include_adult=True, force=False
    )
    assert changed is True
    assert isinstance(mapping_params, dict)
    assert mapping_params["api_key"] == "k"
    assert mapping_params["page"] == "1"
    assert mapping_params["include_adult"] == "true"


def test_url_query_is_merged_instead_of_dropped(patcher):
    """When the URL carries its own query string it is folded into the returned params."""
    url = "https://api.themoviedb.org/3/search/movie?api_key=k"

    params, changed = patcher.rewrite_params(url, "query=avatar", include_adult=True, force=False)

    assert changed is True
    assert params == "api_key=k&query=avatar&include_adult=true"


def test_unmatched_urls_return_the_original_params(patcher):
    """An out of scope URL returns the very same params object untouched."""
    mapping = {"query": "avatar"}

    result, changed = patcher.rewrite_params(
        "https://api.themoviedb.org/3/movie/550", mapping, include_adult=True, force=False
    )
    assert changed is False
    assert result is mapping

    text = "query=avatar"
    assert patcher.rewrite_params("https://example.com/3/search/movie", text, True, False) == (text, False)


def test_existing_key_in_params_is_respected(patcher):
    """A caller supplied include_adult=false is not overridden while force is off."""
    url = "https://api.themoviedb.org/3/search/movie"

    text = "query=avatar&include_adult=false"
    assert patcher.rewrite_params(url, text, True, False) == (text, False)

    from_url, changed = patcher.rewrite_params(f"{url}?include_adult=false", None, True, False)
    assert changed is False
    assert from_url is None


def test_percent_encoded_values_survive_the_rewrite(patcher):
    """Encoded separators stay encoded so a value never splits into two parameters."""
    url = "https://api.themoviedb.org/3/search/movie?query=a%26b%2Bc&page=1"

    params, changed = patcher.rewrite_params(url, None, include_adult=True, force=False)

    assert changed is True
    assert "query=a%26b%2Bc" in params
    assert "include_adult=true" in params
    assert "a&b" not in params


def test_plus_encoded_spaces_survive_the_rewrite(patcher):
    """A plus sign keeps its form-urlencoded meaning after re-encoding."""
    url = "https://api.themoviedb.org/3/search/movie?query=avatar+2"

    params, changed = patcher.rewrite_params(url, None, include_adult=True, force=False)

    assert changed is True
    assert "query=avatar+2" in params


# ---------------- prepared request rewrite ----------------


def test_rewrite_url_only_touches_search_requests(patcher):
    """A prepared request URL is rewritten only inside the search scope."""
    url = "https://api.themoviedb.org/3/search/multi?query=a"

    assert patcher.rewrite_url(url, True, False) == (f"{url}&include_adult=true", True)
    detail = "https://api.themoviedb.org/3/movie/550"
    assert patcher.rewrite_url(detail, True, False) == (detail, False)


def test_rewrite_request_moves_url_query_into_params(patcher):
    """The request helper returns a base URL without query plus the merged params."""
    url = "https://api.themoviedb.org/3/search/movie?api_key=k"

    new_url, new_params, changed = patcher.rewrite_request(url, "query=avatar", True, False)

    assert changed is True
    assert new_url == "https://api.themoviedb.org/3/search/movie"
    assert new_params == "api_key=k&query=avatar&include_adult=true"


# ---------------- disabled install ----------------


def test_disabled_patcher_touches_nothing(patcher):
    """A disabled patcher reports skipped and installs no wrapper at all."""
    instance = patcher.SearchParamPatcher(enabled=False)

    result = instance.install()
    assert result.status == "skipped"
    assert result.installed is False
    assert instance.installed is False
    assert instance.originals == ()

    instance.uninstall()
    assert instance.hits == 0


def test_install_result_reports_partial_state(patcher):
    """The result object exposes whether at least one layer is wrapped."""
    assert patcher.InstallResult("installed", ("a",), ()).installed is True
    assert patcher.InstallResult("partial", ("a",), ("issue",)).installed is True
    assert patcher.InstallResult("unsupported", (), ("issue",)).installed is False
