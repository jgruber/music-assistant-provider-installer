"""
Custom Provider Installer — a Music Assistant plugin that fetches providers
from GitHub and drops them into the fork's custom_providers directory.

Workflow
--------
1. Operator adds one or more repository URLs to the plugin's config (one
   URL per line, each optionally suffixed with ``@branch-or-tag``).
2. On every setup — which runs when the config is saved and on every MA
   restart — this plugin downloads each URL's archive, extracts the
   top-level provider directory into ``<storage_path>/custom_providers/``,
   and ``uv pip install`` the merged union of every manifest's
   ``requirements``.
3. Newly-installed providers do not appear in the running session (MA's
   provider-manifest walk runs once at startup, before any plugin runs).
   The operator restarts Music Assistant; the fork's custom-providers
   loader finds the dropped directories on the next startup.

This plugin only installs providers; it never removes or modifies files it
did not create. Removing an entry from the config does not uninstall; the
operator deletes the directory under ``<storage_path>/custom_providers/``
manually.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from aiohttp import ClientTimeout
from music_assistant.models.plugin import PluginProvider
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest


CONF_REPOS = "repos"

LOGGER = logging.getLogger("music_assistant.provider_installer")

CUSTOM_PROVIDERS_SUBDIR = "custom_providers"
# Match a URL like https://github.com/owner/repo(.git)?(@ref)?
# Groups: 1=owner, 2=repo, 3=ref (optional)
_URL_RE = re.compile(
    r"^https?://github\.com/([^/\s@]+)/([^/\s@]+?)(?:\.git)?(?:@([^\s@]+))?/?$"
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Create the plugin instance."""
    return ProviderInstaller(mass, manifest, config, set())


class ProviderInstaller(PluginProvider):
    """Fetches custom providers from GitHub into the fork's custom_providers dir."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Expose the one config entry: a repeating URL list."""
        return (
            ConfigEntry(
                key=CONF_REPOS,
                type=ConfigEntryType.STRING,
                required=False,
                multi_value=True,
                default_value=[],
            ),
        )

    async def handle_async_init(self) -> None:
        """
        Fetch each configured URL and install it into custom_providers.

        Idempotent: an archive extraction overwrites the existing dir; a
        pip-install of already-present packages is a no-op; a URL that fails
        logs a WARNING but does not block other URLs or the plugin itself.
        """
        raw = self.get_setup_value(CONF_REPOS) or []
        urls = [str(x).strip() for x in raw if str(x).strip()]
        if not urls:
            self.logger.info("No repository URLs configured")
            return

        target = Path(self.mass.storage_path) / CUSTOM_PROVIDERS_SUBDIR
        target.mkdir(parents=True, exist_ok=True)

        all_requirements: list[str] = []
        installed: list[str] = []
        session = self.mass.http_session
        for raw_url in urls:
            try:
                domain, reqs = await self._install_one(session, raw_url, target)
            except Exception as err:
                self.logger.warning(
                    "Install from %r failed: %s", raw_url, err, exc_info=True
                )
                continue
            installed.append(domain)
            all_requirements.extend(reqs)

        if all_requirements:
            await asyncio.to_thread(_pip_install_requirements, all_requirements)

        if installed:
            self.logger.info(
                "Installed/refreshed custom providers: %s. "
                "Restart Music Assistant to activate.",
                ", ".join(installed),
            )

    async def _install_one(
        self, session: ClientSession, raw_url: str, target: Path
    ) -> tuple[str, list[str]]:
        """Fetch one URL, extract its top-level provider dir, return (domain, reqs)."""
        owner, repo, ref = _parse_github_url(raw_url)
        ref = ref or "main"
        # heads/<ref> works for both branches and tags on GitHub archives;
        # tags are also reachable at tags/<ref> if the first form 404s.
        candidates = [
            f"https://github.com/{owner}/{repo}/archive/refs/heads/{ref}.tar.gz",
            f"https://github.com/{owner}/{repo}/archive/refs/tags/{ref}.tar.gz",
        ]
        last_err: Exception | None = None
        for archive_url in candidates:
            try:
                archive_bytes = await _fetch(session, archive_url)
                break
            except Exception as err:  # noqa: BLE001
                last_err = err
        else:  # all candidates failed
            raise RuntimeError(f"download failed: {last_err}")

        domain, reqs = await asyncio.to_thread(
            _extract_and_install_dir, archive_bytes, target
        )
        self.logger.info(
            "Fetched %s/%s@%s -> %s (%d requirement(s))",
            owner,
            repo,
            ref,
            target / domain,
            len(reqs),
        )
        return domain, reqs


# ---------- helpers (no `self`; run in a thread for the sync tarfile work) ----------


def _parse_github_url(raw: str) -> tuple[str, str, str | None]:
    """Return (owner, repo, ref-or-None) for a GitHub URL."""
    # Allow an owner/repo shorthand too.
    if "/" in raw and "://" not in raw:
        parts = raw.split("@", 1)
        owner_repo = parts[0]
        ref = parts[1] if len(parts) == 2 else None
        if owner_repo.count("/") == 1:
            owner, repo = owner_repo.split("/", 1)
            return owner, repo.removesuffix(".git"), ref
    match = _URL_RE.match(raw)
    if not match:
        parsed = urlparse(raw)
        raise ValueError(f"not a GitHub repository URL: {parsed.geturl()!r}")
    return match.group(1), match.group(2), match.group(3)


#: Hard cap on a single archive download so a misconfigured URL cannot wedge
#: MA start-up on a huge file. GitHub-hosted provider archives are KB-to-low-MB
#: in practice.
_MAX_ARCHIVE_BYTES = 50 * 1024 * 1024


async def _fetch(session: ClientSession, url: str) -> bytes:
    """GET the URL as bytes, raising on non-2xx or on exceeding the size cap."""
    async with session.get(url, timeout=ClientTimeout(total=60)) as resp:
        resp.raise_for_status()
        content_length = resp.content_length
        if content_length is not None and content_length > _MAX_ARCHIVE_BYTES:
            raise RuntimeError(
                f"archive at {url} declares {content_length} bytes, "
                f"over the {_MAX_ARCHIVE_BYTES} byte cap"
            )
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > _MAX_ARCHIVE_BYTES:
                raise RuntimeError(
                    f"archive at {url} exceeded the {_MAX_ARCHIVE_BYTES} byte cap"
                )
            chunks.append(chunk)
        return b"".join(chunks)


def _extract_and_install_dir(
    archive_bytes: bytes, target: Path
) -> tuple[str, list[str]]:
    """
    Extract the top-level provider directory from a GitHub archive tarball
    into ``target/<domain>/`` and return (domain, manifest-requirements).

    GitHub archives contain exactly one top-level directory named
    ``<repo>-<ref>``. Inside that, we look for the first subdirectory that
    carries a ``manifest.json`` — that is the provider directory. Everything
    else (LICENSE, README, workflows) is ignored.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(fileobj=_bytes_io(archive_bytes), mode="r:gz") as tf:
            # Python 3.14 defaults to data filter; be explicit for older runtimes too.
            tf.extractall(tmp_path, filter="data")  # type: ignore[arg-type]
        tops = [p for p in tmp_path.iterdir() if p.is_dir()]
        if len(tops) != 1:
            raise RuntimeError(f"archive has {len(tops)} top-level entries, expected 1")
        root = tops[0]

        provider_dirs = [
            p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").is_file()
        ]
        if not provider_dirs:
            raise RuntimeError(
                f"no provider directory with manifest.json inside {root.name!r}"
            )
        if len(provider_dirs) > 1:
            names = ", ".join(sorted(p.name for p in provider_dirs))
            raise RuntimeError(
                f"archive contains multiple providers ({names}); refusing"
            )
        src = provider_dirs[0]

        with (src / "manifest.json").open(encoding="utf-8") as f:
            manifest_data = json.load(f)
        domain = manifest_data.get("domain")
        if not isinstance(domain, str) or not domain:
            raise RuntimeError(f"manifest.json in {src.name!r} has no domain")
        if domain != src.name:
            raise RuntimeError(
                f"manifest.json domain {domain!r} does not match directory name {src.name!r}"
            )
        requirements = [str(r) for r in (manifest_data.get("requirements") or [])]

        dest = target / domain
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(src), str(dest))
        return domain, requirements


def _bytes_io(data: bytes):
    from io import BytesIO

    return BytesIO(data)


def _pip_install_requirements(requirements: list[str]) -> None:
    """
    Install the merged requirements into the Music Assistant venv.

    Uses ``uv pip install`` through the venv's own uv binary. ``/app/venv``
    is the fork image's layout; in a dev install this falls back to using
    the ``uv`` on PATH against the active sys.prefix.
    """
    import subprocess
    import sys

    deduped: list[str] = []
    seen: set[str] = set()
    for r in requirements:
        if r not in seen:
            seen.add(r)
            deduped.append(r)

    uv = _find_uv()
    if not uv:
        LOGGER.warning(
            "uv not found; cannot install custom-provider requirements. "
            "Providers whose deps are not already in the venv will fail to import."
        )
        return
    LOGGER.info(
        "Installing %d custom-provider requirement(s): %s", len(deduped), deduped
    )
    try:
        subprocess.run(
            [uv, "pip", "install", "--python", sys.executable, *deduped],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as err:
        LOGGER.warning(
            "uv pip install failed (returncode %d). stdout:\n%s\nstderr:\n%s",
            err.returncode,
            err.stdout,
            err.stderr,
        )


def _find_uv() -> str | None:
    """Return the uv binary path, preferring the venv's own copy."""
    import sys

    candidate = Path(sys.prefix) / "bin" / "uv"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which("uv")
