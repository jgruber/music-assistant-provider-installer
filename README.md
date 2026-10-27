# music-assistant-provider-installer

A Music Assistant plugin that fetches custom providers from GitHub and
drops them into the fork's `custom_providers/` directory.

Requires [`jgruber/music-assistant-server`](https://github.com/jgruber/music-assistant-server)
(the soft fork with the HACS-style custom-providers loader). On stock
upstream Music Assistant it is a no-op — upstream has no mechanism for
loading providers out of tree.

## How it works

1. Install this plugin itself as a custom provider (drop the
   `provider_installer/` directory into `<storage_path>/custom_providers/`
   and restart MA).
2. In the Music Assistant UI, go to **Settings → Providers → Add provider**
   and pick **Custom Provider Installer**.
3. Add one or more GitHub URLs. Each entry is a URL like
   `https://github.com/owner/repo`, optionally suffixed with `@branch-or-tag`
   (defaults to `main`).
4. Save. The plugin's setup runs: for each URL, it downloads the archive,
   extracts the top-level provider directory into
   `<storage_path>/custom_providers/<domain>/`, and `uv pip install`s the
   union of every provider's manifest `requirements`.
5. **Restart Music Assistant.** The fork's custom-providers loader runs at
   startup and the newly-installed providers appear in the normal "Add
   Music Source" / "Add Player" UI alongside the in-tree ones.

The restart is required: MA's provider manifest walk runs once at startup
before any plugin's `setup()` is reachable, so a provider installed at
runtime cannot appear in the current session.

## Repository shape expected

The plugin accepts any GitHub repo that contains a single provider
directory at its root — the directory holding `manifest.json`,
`__init__.py` and the usual provider files, named exactly the same as the
`domain` in that manifest. Examples:

```
my-cool-player/
├── LICENSE
├── README.md
└── my_cool_player/
    ├── manifest.json          <- "domain": "my_cool_player"
    ├── __init__.py
    └── strings.json
```

Multi-provider repos (two providers in one archive) are refused for safety;
split them into one repo per provider.

## Removing a provider

The plugin only installs; it does not uninstall. To remove a provider:

1. Remove its URL from the plugin's config (prevents re-install on next
   setup).
2. Delete the directory under `<storage_path>/custom_providers/<domain>/`.
3. Restart Music Assistant.

The provider's pip dependencies stay in the venv — they are shared across
providers, so leaving them in place is safe and faster than re-resolving
on every install.

## Security

Any custom provider — one you drop in by hand, one this plugin fetches —
runs with the full server's privileges. The fork emits a WARNING on each
load. Only install repositories you trust.

## License

Apache-2.0.
