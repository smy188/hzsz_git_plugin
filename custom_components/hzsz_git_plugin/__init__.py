"""hzsz_git_plugin — download all component folders from a Git repo
into Home Assistant's custom_components directory.

Guides the user to fill in:
  - Git repository URL (e.g. https://github.com/user/repo.git)
  - Username / Password (for private repos)
  - Branch / Tag (to target a specific version)
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from git import GitCommandError, Repo
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)

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

PLATFORMS: list[str] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_authenticated_url(
    repo_url: str,
    username: str | None,
    password: str | None,
) -> str:
    """Insert username + password into the URL for HTTP-based Git repos."""
    if not username and not password:
        return repo_url

    if repo_url.startswith("http://") or repo_url.startswith("https://"):
        protocol, rest = repo_url.split("://", 1)
        # RFC-compliant: encode special characters in credentials
        from urllib.parse import quote

        user_part = quote(username or "", safe="")
        pass_part = quote(password or "", safe="")
        return f"{protocol}://{user_part}:{pass_part}@{rest}"

    # For non-HTTP URLs (e.g., SSH), we can't embed creds — return as-is
    return repo_url


def _repo_dir_name(repo_url: str) -> str:
    """Build a safe directory name from the repository URL."""
    raw_name = repo_url.rstrip("/").removesuffix(".git").split("/")[-1]
    safe_name = "".join(
        char if char.isalnum() or char in "._-" else "_"
        for char in raw_name
    )
    return safe_name or "repository"


def _clone_and_install(
    repo_url: str,
    username: str | None,
    password: str | None,
    branch: str | None,
    delete_existing: bool,
    custom_components_dir: Path,
) -> tuple[bool, str, list[str]]:
    """Clone the Git repository to a temp directory, then copy all contents
    into custom_components (like ``cp -rf repo_contents/* custom_components/``).

    Returns (success, message, list_of_installed_items).
    """
    installed: list[str] = []
    temp_dir: Path | None = None

    try:
        custom_components_dir.mkdir(parents=True, exist_ok=True)
        clone_url = _build_authenticated_url(repo_url, username, password)
        repo_name = _repo_dir_name(repo_url)

        # Clone to a temporary directory first (NOT directly into custom_components)
        temp_dir = custom_components_dir / f".{repo_name}_tmp"

        if temp_dir.exists():
            _LOGGER.info("Removing stale temp directory: %s", temp_dir)
            shutil.rmtree(temp_dir, ignore_errors=True)

        _LOGGER.info("Cloning repository to temp directory %s", temp_dir)

        clone_kwargs: dict = {
            "url": clone_url,
            "to_path": temp_dir,
            "depth": 1,
        }
        if branch:
            clone_kwargs["branch"] = branch

        Repo.clone_from(**clone_kwargs)
        _LOGGER.info("Clone successful")

        # Copy all contents (except .git) from temp_dir into custom_components_dir
        for item in temp_dir.iterdir():
            if item.name == ".git":
                continue
            dest = custom_components_dir / item.name
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest, ignore_errors=True)
                else:
                    dest.unlink()
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
            installed.append(item.name)

        _LOGGER.info("Copied %d items to %s: %s", len(installed), custom_components_dir, installed)

        return True, f"成功安装组件到 custom_components: {', '.join(installed)}", installed

    except GitCommandError as exc:
        error_msg = str(exc)
        _LOGGER.error("Git clone failed: %s", error_msg.split("\n")[0].split(": ")[-1] if ": " in error_msg else error_msg[:200])
        error_msg = str(exc)
        if "Authentication" in error_msg or "auth" in error_msg.lower():
            return False, "认证失败，请检查用户名和密码", []
        if "not found" in error_msg.lower() or "404" in error_msg:
            return False, "仓库未找到，请检查 URL 是否正确", []
        if "Remote branch" in error_msg or "not found in upstream" in error_msg:
            return False, f"分支 '{branch}' 不存在", []
        return False, f"Git 克隆失败: {error_msg}", []

    except Exception as exc:
        _LOGGER.exception("Unexpected error")
        return False, f"安装失败: {str(exc)}", []

    finally:
        # Always clean up the temp directory
        if temp_dir is not None and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
            _LOGGER.info("Cleaned up temp directory: %s", temp_dir)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

def _get_merged_data(entry: ConfigEntry) -> dict:
    """Merge entry.data (from initial config flow) with entry.options
    (from options flow updates)."""
    return {**entry.data, **entry.options}


async def _handle_install_service(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler: trigger re-download for a specific entry or all entries."""
    entry_id = call.data.get("entry_id", "")

    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        _LOGGER.warning("No hzsz_git_plugin config entry found")
        return

    if entry_id:
        # Install a specific entry
        target = hass.config_entries.async_get_entry(entry_id)
        if target is None or target.domain != DOMAIN:
            _LOGGER.warning("Entry not found: %s", entry_id)
            return
        await _do_install(hass, _get_merged_data(target))
    else:
        # Install all configured entries
        for entry in entries:
            await _do_install(hass, _get_merged_data(entry))


async def _do_install(hass: HomeAssistant, data: dict) -> bool:
    """Execute the actual clone-and-install logic in the executor pool."""
    repo_url = data.get(CONF_REPO_URL, "")
    username = data.get(CONF_USERNAME) or None
    password = data.get(CONF_PASSWORD) or None
    branch = data.get(CONF_BRANCH) or None  # None → use repo default
    ref_type = data.get(CONF_REF_TYPE, "")
    delete_existing = data.get(CONF_DELETE_EXISTING, False)

    custom_components_dir = Path(hass.config.path("custom_components"))

    # Use a unique notification_id per repo to avoid collisions
    safe_repo = _repo_dir_name(repo_url)
    notif_id_success = f"hzsz_git_plugin_success_{safe_repo}"
    notif_id_error = f"hzsz_git_plugin_error_{safe_repo}"

    _LOGGER.info(
        "Starting install from repo=%s branch=%s ref_type=%s delete_existing=%s",
        repo_url,
        branch or "(default)",
        ref_type or "(none)",
        delete_existing,
    )

    success, message, installed = await hass.async_add_executor_job(
        _clone_and_install,
        repo_url,
        username,
        password,
        branch,
        delete_existing,
        custom_components_dir,
    )

    if success:
        _LOGGER.info("Install successful: %s", message)

        # Accumulate pending-restart items so multiple entries
        # produce only a single "restart required" repair issue.
        pending: list[str] = hass.data.setdefault(DOMAIN, {}).setdefault(
            "pending_restart", []
        )
        pending.append(
            f"{safe_repo}（{', '.join(installed)}）"
        )

        # Replace the single restart-required issue with updated info
        async_delete_issue(hass, DOMAIN, "restart_required")
        async_create_issue(
            hass,
            DOMAIN,
            "restart_required",
            is_fixable=True,
            severity=IssueSeverity.WARNING,
            translation_key="restart_required",
            translation_placeholders={
                "name": "\n".join(f"  • {item}" for item in pending),
            },
        )

        # Also fire a success notification
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": f"hzsz_git_plugin — {safe_repo}",
                "message": f"✅ {message}",
                "notification_id": notif_id_success,
            },
            blocking=False,
        )
    else:
        _LOGGER.error("Install failed: %s", message)
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": f"hzsz_git_plugin — {safe_repo} 安装失败",
                "message": f"❌ {message}",
                "notification_id": notif_id_error,
            },
            blocking=False,
        )
    return success


# ---------------------------------------------------------------------------
# Integration lifecycle
# ---------------------------------------------------------------------------


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """YAML-based setup (not supported — use the UI config flow)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up hzsz_git_plugin from a config entry.

    Multiple entries are supported — each one is an independent Git
    repository configuration.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})

    # Clean up any stale "restart required" issue leftover from a previous
    # restart. If another entry has just been installed in this runtime, the
    # pending_restart list should stay visible.
    if "pending_restart" not in domain_data:
        async_delete_issue(hass, DOMAIN, "restart_required")

    # Register the service once.
    if not hass.services.has_service(DOMAIN, "install"):
        async def _service_handler(call: ServiceCall) -> None:
            await _handle_install_service(hass, call)

        hass.services.async_register(DOMAIN, "install", _service_handler)

    reload_entries: set[str] = domain_data.setdefault("reload_entries", set())
    should_install = (
        not entry.data.get(CONF_INSTALLED, False)
        or entry.entry_id in reload_entries
    )
    reload_entries.discard(entry.entry_id)

    if should_install:
        if await _do_install(hass, _get_merged_data(entry)):
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_INSTALLED: True},
            )

    # Register the update listener for options changes
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    # Store reference so we know this entry is active
    domain_data[entry.entry_id] = entry

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        domain_data = hass.data.get(DOMAIN, {})
        if hass.is_running:
            domain_data.setdefault("reload_entries", set()).add(entry.entry_id)
        domain_data.pop(entry.entry_id, None)
        active_entries = [
            key for key in domain_data
            if key not in ("pending_restart", "reload_entries")
        ]
        if (
            not active_entries
            and "pending_restart" not in domain_data
            and "reload_entries" not in domain_data
        ):
            hass.data.pop(DOMAIN, None)

    return unload_ok


async def async_update_options(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update — re-download components with new settings."""
    _LOGGER.info("Options updated, re-downloading components...")
    await _do_install(hass, _get_merged_data(entry))
