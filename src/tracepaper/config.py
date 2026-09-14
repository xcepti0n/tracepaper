"""Configuration. All tunables live here or in a TOML file, never inline."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

# Synology and macOS scatter these through every share; they are never content.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "@eaDir",           # Synology thumbnail/index sidecars
    "#recycle",         # Synology recycle bin
    "#snapshot",
    ".DS_Store",
    ".Trashes",
    ".Spotlight-V100",
    ".fseventsd",
    "__MACOSX",
    ".git",
    "@tmp",
    "desktop.ini",
    "Thumbs.db",
    # Developer and application noise. A single node_modules can be tens of
    # thousands of files, none of which anyone will ever search for, and it
    # costs a stat and a row each on every scan.
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".cache",
    "Caches",
    ".Trash",
    "@Recycle",
    # Application internals that live *inside* document folders. A note vault
    # keeps its plugins beside the notes, so the notes are worth indexing and
    # the bundled JavaScript is not -- a single plugin's main.js is minified
    # code that matches half the English language and outranks nothing useful.
    ".obsidian",
    ".trash",            # Obsidian's own, lowercase
    ".stfolder",         # Syncthing
    ".stversions",
    ".dropbox.cache",
    ".idea",
    ".vscode",
    "site-packages",
    "dist-packages",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".gradle",
    # Packaging output. A single PyInstaller build drops hundreds of METADATA,
    # RECORD and LICENSE files with no extension, which look like documents to
    # anything reading the filename -- one project's `dist/` filled an entire
    # page of results for "3d printer".
    #
    # `_internal` is PyInstaller's own; the `*.dist-info` and `*.egg-info`
    # directories beside it are matched by pattern, not by this list, since
    # their names carry a version (see scan.scanner).
    "_internal",
    "egg-info",
    # NOT "dist" or "build" on their own. Those are ordinary English words and
    # a folder named either could hold real documents. Search still hides
    # anything under them (query/modes.py) -- indexing is the cheaper mistake
    # to make, and it stays reversible.
)

# Directory name patterns excluded from scanning, matched with fnmatch against
# the directory name. For generated names a fixed list cannot hold.
DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "*.dist-info",
    "*.egg-info",
)

# Soft-delete only after this many consecutive scans miss a path. An unmounted
# NAS makes every file vanish at once; this stops one bad scan wiping the index.
DEFAULT_MISS_THRESHOLD = 3

# Below this fraction of previously-known paths still visible, a scan aborts
# rather than mass-deleting. Catches a share that failed to mount.
DEFAULT_VANISH_GUARD = 0.5


@dataclass(frozen=True)
class Config:
    # Where the index is written. Kept separate from the scanned roots so the
    # source tree stays strictly read-only (NFR-7).
    db_path: Path = Path("data/index.db")
    # Where backups and exports are written -- the NAS is the right home, since
    # that is the copy that survives losing the index machine.
    backup_dir: Path | None = None
    roots: tuple[Path, ...] = ()
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES
    exclude_patterns: tuple[str, ...] = DEFAULT_EXCLUDE_PATTERNS
    max_file_bytes: int = 512 * 1024 * 1024
    miss_threshold: int = DEFAULT_MISS_THRESHOLD
    vanish_guard: float = DEFAULT_VANISH_GUARD
    follow_symlinks: bool = False
    passage_target_chars: int = 1200
    passage_overlap_chars: int = 150

    # LLM gap-filling at ingest (M6). Off unless explicitly enabled.
    llm_enabled: bool = False
    llm_endpoint: str = "http://localhost:11434"
    llm_model: str = "gemma4:e4b-mlx"
    llm_timeout: int = 120
    # Only documents yielding fewer than this many fields go to the model, so
    # the expensive layer runs on the documents that actually need it.
    llm_min_fields: int = 3

    embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # Vision model for photo captions, used only by the background enricher.
    vlm_model: str = "gemma4:e4b"
    # Enrichment yields above this 1-minute load average per core.
    enrich_load_threshold: float = 0.7

    extra: dict = field(default_factory=dict)

    # The file this was loaded from, so anything saving settings writes back to
    # the file the service actually reads. Without it the API guessed a
    # relative "tracepaper.toml", which resolved against the working directory
    # and tried to write into /opt/tracepaper: a PermissionError at best, and
    # at worst a config the service would never read again.
    source_path: Path | None = None

    @staticmethod
    def load(path: Path | str | None = None) -> "Config":
        cfg = Config()
        if path is None:
            for candidate in (Path("tracepaper.toml"), Path("config/tracepaper.toml")):
                if candidate.exists():
                    path = candidate
                    break
        if path is None:
            return cfg

        cfg = replace(cfg, source_path=Path(path))
        data = tomllib.loads(Path(path).read_text())
        idx = data.get("index", {})
        scan = data.get("scan", {})

        updates: dict = {}
        if "db_path" in idx:
            updates["db_path"] = Path(idx["db_path"]).expanduser()
        if "backup_dir" in idx:
            updates["backup_dir"] = Path(idx["backup_dir"]).expanduser()
        if "roots" in scan:
            updates["roots"] = tuple(Path(r).expanduser() for r in scan["roots"])
        if "excludes" in scan:
            # MERGE, never replace. A config written months ago cannot know
            # about a name added since, and replacing the list silently dropped
            # every default -- which is how .obsidian plugin JavaScript ended up
            # indexed on a host whose config predated that exclude.
            #
            # Prefix a name with "!" to genuinely un-exclude a default, so the
            # escape hatch stays available but has to be asked for.
            configured = [str(x) for x in scan["excludes"]]
            keep = {name[1:] for name in configured if name.startswith("!")}
            added = [name for name in configured if not name.startswith("!")]
            merged = [name for name in DEFAULT_EXCLUDES if name not in keep]
            for name in added:
                if name not in merged:
                    merged.append(name)
            updates["excludes"] = tuple(merged)
        for key in ("max_file_bytes", "miss_threshold", "vanish_guard",
                    "follow_symlinks", "passage_target_chars",
                    "passage_overlap_chars"):
            if key in scan:
                updates[key] = scan[key]

        llm = data.get("llm", {})
        for toml_key, cfg_key in (("enabled", "llm_enabled"),
                                  ("endpoint", "llm_endpoint"),
                                  ("model", "llm_model"),
                                  ("timeout", "llm_timeout"),
                                  ("min_fields", "llm_min_fields")):
            if toml_key in llm:
                updates[cfg_key] = llm[toml_key]

        semantic = data.get("semantic", {})
        if "model" in semantic:
            updates["embed_model"] = semantic["model"]

        enrich = data.get("enrich", {})
        if "vlm_model" in enrich:
            updates["vlm_model"] = enrich["vlm_model"]
        if "load_threshold" in enrich:
            updates["enrich_load_threshold"] = enrich["load_threshold"]

        return replace(cfg, **updates, extra=data)
