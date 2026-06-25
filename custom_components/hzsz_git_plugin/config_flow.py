"""Config flow for hzsz_git_plugin.

Two-step interaction:
  1. Enter repo URL + optional username/password → fetch branches & tags.
  2. Pick from a combined dropdown (default / branches / tags) → install.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import voluptuous as vol
from git.cmd import Git
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_BRANCH,
    CONF_DELETE_EXISTING,
    CONF_INSTALLED,
    CONF_PASSWORD,
    CONF_REF_TYPE,
    CONF_REPO_URL,
    CONF_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Dropdown sentinels (<key>:<name>)
_KEY_DEFAULT = "##default"
_PREFIX_BRANCH = "branch:"
_PREFIX_TAG = "tag:"


# ---------------------------------------------------------------------------
# Helpers (run in executor)
# ---------------------------------------------------------------------------


def _build_auth_url(repo_url: str, username: str | None, password: str | None) -> str:
    """Insert credentials into an HTTP(S) Git URL."""
    if not username and not password:
        return repo_url
    if repo_url.startswith("http://") or repo_url.startswith("https://"):
        protocol, rest = repo_url.split("://", 1)
        return f"{protocol}://{quote(username or '', safe='')}:{quote(password or '', safe='')}@{rest}"
    return repo_url


def _fetch_remote_refs(
    repo_url: str, username: str | None, password: str | None
) -> tuple[bool, list[str], list[str], str]:
    """Use ``git ls-remote`` to list branches and tags.

    Returns (success, branches, tags, message).
    """
    try:
        auth_url = _build_auth_url(repo_url, username, password)
        git = Git()
        output = git.ls_remote(auth_url)

        branches: list[str] = []
        tags: list[str] = []

        for line in output.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            refname = parts[1]

            if refname.startswith("refs/heads/"):
                branches.append(refname[len("refs/heads/"):])
            elif refname.startswith("refs/tags/"):
                tag = refname[len("refs/tags/"):]
                if not tag.endswith("^{}"):
                    tags.append(tag)

        branches.sort()
        tags.sort()

        total = len(branches) + len(tags)
        if total == 0:
            return False, [], [], "仓库中没有找到任何分支或标签"

        msg = f"找到 {len(branches)} 个分支, {len(tags)} 个标签"
        return True, branches, tags, msg

    except Exception as exc:
        error_msg = str(exc)
        _LOGGER.error("Failed to fetch remote refs: %s", error_msg.split("\n")[0][:200])
        msg = str(exc)
        if "Authentication" in msg or "auth" in msg.lower():
            return False, [], [], "认证失败，请检查用户名和密码是否正确"
        if "not found" in msg.lower() or "404" in msg:
            return False, [], [], "仓库未找到，请检查仓库 URL 是否正确"
        if "resolve host" in msg.lower() or "connection" in msg.lower():
            return False, [], [], f"无法连接到仓库服务器: {msg}"
        return False, [], [], f"连接失败: {msg}"


# ---------------------------------------------------------------------------
# Config Flow — 2 steps
# ---------------------------------------------------------------------------


def _repo_name(url: str) -> str:
    """Extract a safe repository name from the URL."""
    name = url.rstrip("/").removesuffix(".git").split("/")[-1]
    return name or "repository"


class HzszGitPluginConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Two-step config flow with a combined branch/tag dropdown in step 2.

    ┌──────────────┬────────────────────┬───────────────────────────┐
    │  Step 1      │  repo_url + auth   │  git ls-remote → refs     │
    │  Step 2      │  select ref        │  default + branches       │
    │              │                    │  + tags in one dropdown   │
    └──────────────┴────────────────────┴───────────────────────────┘
    """

    VERSION = 1

    def __init__(self) -> None:
        self._errors: dict[str, str] = {}
        self._branches: list[str] = []
        self._tags: list[str] = []
        self._user_input: dict[str, Any] = {}
        self._fetch_message: str = ""

    # ------------------------------------------------------------------
    # Step 1 — Repo URL + credentials → fetch refs
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: collect repo URL & credentials, then fetch refs."""
        self._errors = {}

        if user_input is not None:
            repo_url = user_input.get(CONF_REPO_URL, "").strip()
            username = user_input.get(CONF_USERNAME, "").strip()
            password = user_input.get(CONF_PASSWORD, "").strip()

            if not repo_url:
                self._errors[CONF_REPO_URL] = "repo_url_required"
                return await self._show_step_user(user_input)

            if not (repo_url.startswith("http") or repo_url.startswith("git@")):
                self._errors[CONF_REPO_URL] = "invalid_url_format"
                return await self._show_step_user(user_input)

            success, branches, tags, msg = await self.hass.async_add_executor_job(
                _fetch_remote_refs, repo_url, username or None, password or None
            )

            if not success:
                self._errors["base"] = "cannot_connect"
                self._errors["connection_message"] = msg
                return await self._show_step_user(user_input)

            self._user_input = {
                CONF_REPO_URL: repo_url,
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
            }
            self._branches = branches
            self._tags = tags
            self._fetch_message = msg

            return await self.async_step_select_ref()

        return await self._show_step_user(user_input)

    async def _show_step_user(
        self, user_input: dict[str, Any] | None
    ) -> FlowResult:
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_REPO_URL,
                    default=(user_input or {}).get(CONF_REPO_URL, ""),
                ): str,
                vol.Optional(
                    CONF_USERNAME,
                    default=(user_input or {}).get(CONF_USERNAME, ""),
                ): str,
                vol.Optional(
                    CONF_PASSWORD,
                    default=(user_input or {}).get(CONF_PASSWORD, ""),
                ): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=self._errors
        )

    # ------------------------------------------------------------------
    # Step 2 — Combined dropdown: default + branches + tags
    # ------------------------------------------------------------------

    async def async_step_select_ref(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: pick from one dropdown containing default, every branch,
        and every tag."""
        self._errors = {}

        if user_input is not None:
            selected = user_input.get(CONF_BRANCH, "")

            if not selected:
                self._errors[CONF_BRANCH] = "select_ref"
                return await self._show_step_select_ref()

            # Parse the sentinel key back into ref_type + branch name
            if selected == _KEY_DEFAULT:
                branch, ref_type = "", "default"
            elif selected.startswith(_PREFIX_BRANCH):
                branch, ref_type = selected[len(_PREFIX_BRANCH):], "branch"
            else:
                branch, ref_type = selected[len(_PREFIX_TAG):], "tag"

            return self._build_entry(user_input, branch, ref_type)

        return await self._show_step_select_ref()

    async def _show_step_select_ref(self) -> FlowResult:
        """Build the combined dropdown."""

        options: dict[str, str] = {
            _KEY_DEFAULT: "📌 使用仓库默认分支",
        }
        for b in self._branches:
            options[f"{_PREFIX_BRANCH}{b}"] = f"🌿 {b}  [分支]"
        for t in self._tags:
            options[f"{_PREFIX_TAG}{t}"] = f"🏷️ {t}  [标签]"

        data_schema = vol.Schema(
            {
                vol.Required(CONF_BRANCH, default=_KEY_DEFAULT): vol.In(options),
                vol.Optional(CONF_DELETE_EXISTING, default=False): bool,
            }
        )

        return self.async_show_form(
            step_id="select_ref",
            data_schema=data_schema,
            errors=self._errors,
            description_placeholders={
                "repo_url": self._user_input.get(CONF_REPO_URL, ""),
                "branch_count": str(len(self._branches)),
                "tag_count": str(len(self._tags)),
                "fetch_message": self._fetch_message,
            },
        )

    # ------------------------------------------------------------------
    # Build entry
    # ------------------------------------------------------------------

    def _build_entry(
        self, user_input: dict[str, Any], branch: str, ref_type: str
    ) -> FlowResult:
        """Create the config entry with a descriptive title."""
        repo_url = self._user_input[CONF_REPO_URL]
        data = {
            CONF_REPO_URL: repo_url,
            CONF_USERNAME: self._user_input.get(CONF_USERNAME, ""),
            CONF_PASSWORD: self._user_input.get(CONF_PASSWORD, ""),
            CONF_BRANCH: branch,
            CONF_REF_TYPE: ref_type,
            CONF_DELETE_EXISTING: user_input.get(CONF_DELETE_EXISTING, False),
            CONF_INSTALLED: False,
        }

        repo_name = _repo_name(repo_url)
        if ref_type == "default":
            title = f"{repo_name} — 📌 默认分支"
        elif ref_type == "branch":
            title = f"{repo_name} — 🌿 分支: {branch}"
        else:
            title = f"{repo_name} — 🏷️ 标签: {branch}"

        return self.async_create_entry(
            title=title,
            data=data,
        )

    # ------------------------------------------------------------------
    # Options flow
    # ------------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return HzszGitPluginOptionsFlowHandler()


# ---------------------------------------------------------------------------
# Options Flow (same two-step logic)
# ---------------------------------------------------------------------------


class HzszGitPluginOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options update — same two-step flow."""

    def __init__(self) -> None:
        self._errors: dict[str, str] = {}
        self._branches: list[str] = []
        self._tags: list[str] = []
        self._user_input: dict[str, Any] = {}
        self._fetch_message: str = ""

    def _current_config(self) -> dict[str, Any]:
        """Return this entry's latest saved configuration."""
        return {**self.config_entry.data, **self.config_entry.options}

    # -- Step 1 ----------------------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._errors = {}

        if user_input is not None:
            repo_url = user_input.get(CONF_REPO_URL, "").strip()
            username = user_input.get(CONF_USERNAME, "").strip()
            password = user_input.get(CONF_PASSWORD, "").strip()

            if not repo_url:
                self._errors[CONF_REPO_URL] = "repo_url_required"
                return await self._show_user(user_input)

            if not (repo_url.startswith("http") or repo_url.startswith("git@")):
                self._errors[CONF_REPO_URL] = "invalid_url_format"
                return await self._show_user(user_input)

            success, branches, tags, msg = await self.hass.async_add_executor_job(
                _fetch_remote_refs, repo_url, username or None, password or None
            )

            if not success:
                self._errors["base"] = "cannot_connect"
                self._errors["connection_message"] = msg
                return await self._show_user(user_input)

            self._user_input = {
                CONF_REPO_URL: repo_url,
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
            }
            self._branches = branches
            self._tags = tags
            self._fetch_message = msg

            return await self.async_step_select_ref()

        return await self._show_user(self._current_config())

    async def _show_user(self, data: dict[str, Any]) -> FlowResult:
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_REPO_URL, default=data.get(CONF_REPO_URL, "")
                ): str,
                vol.Optional(
                    CONF_USERNAME, default=data.get(CONF_USERNAME, "")
                ): str,
                vol.Optional(
                    CONF_PASSWORD, default=data.get(CONF_PASSWORD, "")
                ): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=self._errors
        )

    # -- Step 2 ----------------------------------------------------------

    async def async_step_select_ref(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._errors = {}

        if user_input is not None:
            selected = user_input.get(CONF_BRANCH, "")

            if not selected:
                self._errors[CONF_BRANCH] = "select_ref"
                return await self._show_select_ref()

            if selected == _KEY_DEFAULT:
                branch, ref_type = "", "default"
            elif selected.startswith(_PREFIX_BRANCH):
                branch, ref_type = selected[len(_PREFIX_BRANCH):], "branch"
            else:
                branch, ref_type = selected[len(_PREFIX_TAG):], "tag"

            return await self._build_options_entry(user_input, branch, ref_type)

        return await self._show_select_ref()

    async def _show_select_ref(self) -> FlowResult:
        options: dict[str, str] = {_KEY_DEFAULT: "📌 使用仓库默认分支"}
        for b in self._branches:
            options[f"{_PREFIX_BRANCH}{b}"] = f"🌿 {b}  [分支]"
        for t in self._tags:
            options[f"{_PREFIX_TAG}{t}"] = f"🏷️ {t}  [标签]"

        current_config = self._current_config()
        old_branch = current_config.get(CONF_BRANCH, "")
        old_ref_type = current_config.get(CONF_REF_TYPE, "")
        if old_ref_type == "default":
            default_key = _KEY_DEFAULT
        elif old_ref_type == "branch":
            default_key = f"{_PREFIX_BRANCH}{old_branch}"
        elif old_ref_type == "tag":
            default_key = f"{_PREFIX_TAG}{old_branch}"
        else:
            default_key = _KEY_DEFAULT

        if default_key not in options:
            default_key = _KEY_DEFAULT

        data_schema = vol.Schema(
            {
                vol.Required(CONF_BRANCH, default=default_key): vol.In(options),
                vol.Optional(
                    CONF_DELETE_EXISTING,
                    default=current_config.get(CONF_DELETE_EXISTING, False),
                ): bool,
            }
        )

        return self.async_show_form(
            step_id="select_ref",
            data_schema=data_schema,
            errors=self._errors,
            description_placeholders={
                "repo_url": self._user_input.get(CONF_REPO_URL, ""),
                "branch_count": str(len(self._branches)),
                "tag_count": str(len(self._tags)),
                "fetch_message": self._fetch_message,
            },
        )

    # -- Build entry -----------------------------------------------------

    async def _build_options_entry(
        self, user_input: dict[str, Any], branch: str, ref_type: str
    ) -> FlowResult:
        """Build options result and update entry title."""
        repo_url = self._user_input[CONF_REPO_URL]
        repo_name = _repo_name(repo_url)

        if ref_type == "default":
            new_title = f"{repo_name} — 📌 默认分支"
        elif ref_type == "branch":
            new_title = f"{repo_name} — 🌿 分支: {branch}"
        else:
            new_title = f"{repo_name} — 🏷️ 标签: {branch}"

        current_config = self._current_config()

        self.hass.config_entries.async_update_entry(self.config_entry, title=new_title)

        return self.async_create_entry(
            title="",
            data={
                CONF_REPO_URL: repo_url,
                CONF_USERNAME: self._user_input.get(CONF_USERNAME, ""),
                CONF_PASSWORD: self._user_input.get(CONF_PASSWORD, ""),
                CONF_BRANCH: branch,
                CONF_REF_TYPE: ref_type,
                CONF_DELETE_EXISTING: user_input.get(
                    CONF_DELETE_EXISTING,
                    current_config.get(CONF_DELETE_EXISTING, False),
                ),
            },
        )
