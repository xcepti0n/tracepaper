"""What kind of thing a search result is, and which kinds a search wants.

A NAS holds three different things that answer to the same words, and the
right result for "3d printer" depends on which you meant:

    documents  the manual, the receipt, the warranty
    photos     the printer on the desk
    code       a vendored Python file that happens to contain the word

Code is the odd one out. It is not wrong to have indexed it, but it is almost
never what a person searching their own documents is looking for, and there is
a lot of it -- so it is excluded unless asked for. Nothing is deleted: this is
a filter over the index, reversible by a checkbox.

Classification is by file extension, which is deterministic, needs no model,
and needs no re-index.
"""

from __future__ import annotations

from pathlib import Path

# Source and config files. A person searching their documents is not looking
# for these; a person debugging their own project sometimes is, which is why
# the checkbox exists rather than a hard exclusion.
CODE_SUFFIXES = frozenset({
    # Source
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".go",
    ".rs", ".rb", ".php", ".swift", ".m", ".scala", ".sh", ".bash",
    ".zsh", ".fish", ".ps1", ".pl", ".lua", ".r", ".sql", ".vim",
    # Markup and style that is app scaffolding rather than writing
    ".css", ".scss", ".sass", ".less", ".html", ".htm", ".xhtml",
    # Config and data interchange
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".properties", ".lock", ".gradle", ".cmake",
})

# Files with no suffix that are still plainly code or config.
CODE_NAMES = frozenset({
    "makefile", "dockerfile", "vagrantfile", "rakefile", "gemfile",
    "procfile", "jenkinsfile", "cmakelists.txt", ".gitignore",
    ".dockerignore", ".editorconfig", ".gitattributes",
})

# The search modes the UI offers, in the order it offers them.
MODES = ("everything", "documents", "photos", "code")


def is_code(uri: str | None) -> bool:
    """True when a path looks like source or config rather than writing."""
    if not uri:
        return False
    name = Path(uri).name.lower()
    if name in CODE_NAMES:
        return True
    # `.min.js` and friends have two suffixes; Path.suffix sees only the last,
    # which is still `.js`, so a single check covers them.
    return Path(name).suffix in CODE_SUFFIXES


def sql_only_code(alias: str = "i") -> str:
    """The inverse: a predicate matching ONLY code.

    "Code" as a search mode means show me the code, not show me everything
    including code -- so excluding and including are not the two options.
    """
    return f"NOT ({sql_filter(alias)})"


def sql_filter(alias: str = "i") -> str:
    """A SQL predicate excluding code, for use in a WHERE clause.

    Expressed as SQL rather than filtered in Python because the filter has to
    apply *before* LIMIT -- otherwise a page of results that is 90% code comes
    back nearly empty, and the ranking pass has already thrown away the
    documents that would have filled it.

    LIKE with no ESCAPE is fine here: none of the suffixes contain % or _.
    """
    conditions = " OR ".join(
        f"lower({alias}.uri) LIKE '%{suffix}'" for suffix in sorted(CODE_SUFFIXES)
    )
    names = " OR ".join(
        # A trailing-slash match, so `makefile` matches a file called exactly
        # that and not `notes-about-makefile.txt`.
        f"lower({alias}.uri) LIKE '%/{name}'" for name in sorted(CODE_NAMES)
    )
    return f"({alias}.uri IS NULL OR NOT (({conditions}) OR ({names})))"
