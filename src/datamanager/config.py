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
    roots: tuple[Path, ...] = ()
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES
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

    extra: dict = field(default_factory=dict)

    @staticmethod
    def load(path: Path | str | None = None) -> "Config":
        cfg = Config()
        if path is None:
            for candidate in (Path("datamanager.toml"), Path("config/datamanager.toml")):
                if candidate.exists():
                    path = candidate
                    break
        if path is None:
            return cfg

        data = tomllib.loads(Path(path).read_text())
        idx = data.get("index", {})
        scan = data.get("scan", {})

        updates: dict = {}
        if "db_path" in idx:
            updates["db_path"] = Path(idx["db_path"]).expanduser()
        if "roots" in scan:
            updates["roots"] = tuple(Path(r).expanduser() for r in scan["roots"])
        if "excludes" in scan:
            updates["excludes"] = tuple(scan["excludes"])
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

        return replace(cfg, **updates, extra=data)
