"""
title: Forgejo Repository Browser
author: Sven Seeberg
description: List, read and grep files of a single Forgejo repository via the Forgejo API.
requirements: requests, pydantic
version: 0.1.0
license: MIT
"""

import asyncio
import fnmatch
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional
from urllib.parse import quote

import requests
from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo


def _arg(value: Any, fallback: Any) -> Any:
    """Resolve an argument the model did not pass.

    OpenWebUI leaves omitted parameters at their Python default, which for the
    Field() based signatures below is the FieldInfo object itself.
    """
    if isinstance(value, FieldInfo):
        default = value.default
        return fallback if default is Ellipsis else default
    return value


class Tools:
    class Valves(BaseModel):
        base_url: str = Field(
            "https://git.tguerantuer.org",
            description="Base URL of the Forgejo instance, without trailing slash.",
        )
        token: str = Field(
            "",
            description="Forgejo API token. Needs at least the read:repository scope.",
        )
        owner: str = Field(
            "",
            description="Owner (user or organization) of the repository the tool may access.",
        )
        repo: str = Field(
            "",
            description="Name of the single repository the tool may access.",
        )
        default_ref: str = Field(
            "",
            description="Branch, tag or commit used when no ref is given. Empty means the repository default branch.",
        )
        max_file_bytes: int = Field(
            200_000,
            description="Files larger than this are skipped while grepping and truncated while reading.",
        )
        max_grep_files: int = Field(
            2000, description="Maximum number of files fetched during a single grep."
        )
        max_results: int = Field(
            100, description="Maximum number of grep matches returned."
        )
        max_output_chars: int = Field(
            40_000, description="Hard cap on the size of any tool result."
        )
        exclude_paths: str = Field(
            ".git/*,*.lock,*.min.js,*.map,*.png,*.jpg,*.jpeg,*.gif,*.ico,*.pdf,*.zip,*.gz,*.tar,*.woff,*.woff2,*.ttf,*.so,*.bin",
            description="Comma separated glob patterns that are never listed or searched.",
        )
        concurrency: int = Field(
            8, description="Number of parallel file downloads while grepping."
        )
        timeout: int = Field(30, description="HTTP request timeout in seconds.")

    def __init__(self):
        self.valves = self.Valves()
        self.citation = True
        self._tree_cache: dict[str, list[dict]] = {}
        self._default_branch: Optional[str] = None

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _config_error(self) -> Optional[str]:
        missing = [
            name
            for name in ("base_url", "token", "owner", "repo")
            if not getattr(self.valves, name).strip()
        ]
        if missing:
            return (
                "Error: the Forgejo tool is not configured. Missing valve(s): "
                + ", ".join(missing)
            )
        return None

    def _repo_url(self, suffix: str) -> str:
        return (
            f"{self.valves.base_url.rstrip('/')}/api/v1/repos/"
            f"{quote(self.valves.owner)}/{quote(self.valves.repo)}{suffix}"
        )

    def _headers(self) -> dict:
        return {
            "Authorization": f"token {self.valves.token}",
            "Accept": "application/json",
        }

    def _get(self, suffix: str, params: Optional[dict] = None) -> requests.Response:
        response = requests.get(
            self._repo_url(suffix),
            headers=self._headers(),
            params=params or {},
            timeout=self.valves.timeout,
        )
        response.raise_for_status()
        return response

    def _ref(self, ref: str) -> str:
        ref = (ref or "").strip() or self.valves.default_ref.strip()
        if ref:
            return ref
        if self._default_branch is None:
            try:
                self._default_branch = self._get("").json().get("default_branch")
            except Exception:
                self._default_branch = "main"
        return self._default_branch or "main"

    @staticmethod
    def _clean_path(path: str) -> str:
        """Normalize a repository path and reject attempts to escape the repo."""
        path = (path or "").strip().lstrip("/")
        if any(part == ".." for part in path.split("/")):
            raise ValueError("path must not contain '..' segments")
        return path

    def _excluded(self, path: str) -> bool:
        for pattern in self.valves.exclude_paths.split(","):
            pattern = pattern.strip()
            if pattern and fnmatch.fnmatch(path, pattern):
                return True
        return False

    def _truncate(self, text: str) -> str:
        limit = self.valves.max_output_chars
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n[output truncated at {limit} characters]"

    def _tree(self, ref: str) -> list[dict]:
        """Full recursive file list of the given ref, cached per ref."""
        if ref in self._tree_cache:
            return self._tree_cache[ref]

        entries: list[dict] = []
        page = 1
        while True:
            data = self._get(
                f"/git/trees/{quote(ref, safe='')}",
                {"recursive": "true", "page": page, "per_page": 1000},
            ).json()
            entries.extend(data.get("tree") or [])
            total = data.get("total_count") or len(entries)
            if len(entries) >= total or not data.get("tree"):
                break
            page += 1

        files = [e for e in entries if e.get("type") == "blob"]
        self._tree_cache[ref] = files
        return files

    def _raw_file(self, path: str, ref: str) -> bytes:
        return self._get(
            f"/raw/{quote(path)}", {"ref": ref}
        ).content

    @staticmethod
    def _is_binary(blob: bytes) -> bool:
        return b"\x00" in blob[:1024]

    @staticmethod
    def _error(exc: Exception) -> str:
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            status = exc.response.status_code
            if status == 404:
                return "Error: not found (check the path and the ref)."
            if status in (401, 403):
                return "Error: access denied. Check the API token and its scopes."
            return f"Error: Forgejo returned HTTP {status}."
        return f"Error: {exc}"

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------

    def get_repo_info(self) -> str:
        """
        Get metadata about the repository this tool is connected to, including its
        name, description, default branch and whether it is archived.
        """
        error = self._config_error()
        if error:
            return error
        try:
            data = self._get("").json()
        except Exception as exc:
            return self._error(exc)

        lines = [
            f"Repository: {data.get('full_name')}",
            f"Description: {data.get('description') or '(none)'}",
            f"Default branch: {data.get('default_branch')}",
            f"Ref used when none is given: {self._ref('')}",
            f"Size: {data.get('size')} KiB",
            f"Archived: {data.get('archived')}",
            f"URL: {data.get('html_url')}",
        ]
        return "\n".join(lines)

    def list_branches(self) -> str:
        """
        List the branches of the repository. Use this to find a valid ref before
        reading or grepping files on a branch other than the default one.
        """
        error = self._config_error()
        if error:
            return error
        try:
            branches = self._get("/branches", {"limit": 100}).json()
        except Exception as exc:
            return self._error(exc)

        if not branches:
            return "No branches found."
        names = [f"- {b.get('name')}" for b in branches]
        return "Branches:\n" + self._truncate("\n".join(names))

    def list_files(
        self,
        path: str = Field(
            "", description="Directory inside the repository. Empty means the root."
        ),
        ref: str = Field(
            "", description="Branch, tag or commit. Empty means the configured default."
        ),
        recursive: bool = Field(
            False,
            description="If true, list all files below the directory instead of only its direct children.",
        ),
    ) -> str:
        """
        List the files and directories of the repository at a given path. Use this to
        explore the repository structure before reading individual files.
        """
        error = self._config_error()
        if error:
            return error
        path, recursive = _arg(path, ""), bool(_arg(recursive, False))
        ref = self._ref(_arg(ref, ""))
        try:
            path = self._clean_path(path)
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            if recursive:
                prefix = f"{path}/" if path else ""
                entries = [
                    e
                    for e in self._tree(ref)
                    if e["path"].startswith(prefix) and not self._excluded(e["path"])
                ]
                if not entries:
                    return f"No files found under '{path or '/'}' on ref '{ref}'."
                lines = [
                    f"{e['path']} ({e.get('size', 0)} bytes)"
                    for e in sorted(entries, key=lambda e: e["path"])
                ]
                header = f"{len(lines)} file(s) under '{path or '/'}' on ref '{ref}':"
                return self._truncate(header + "\n" + "\n".join(lines))

            contents = self._get(
                f"/contents/{quote(path)}" if path else "/contents", {"ref": ref}
            ).json()
        except Exception as exc:
            return self._error(exc)

        if isinstance(contents, dict):
            return (
                f"'{path}' is a file, not a directory. Use read_file to read it."
            )
        visible = [c for c in contents if not self._excluded(c.get("path", ""))]
        if not visible:
            return f"Directory '{path or '/'}' is empty on ref '{ref}'."

        lines = []
        for entry in sorted(visible, key=lambda c: (c.get("type") != "dir", c["path"])):
            if entry.get("type") == "dir":
                lines.append(f"{entry['path']}/")
            else:
                lines.append(f"{entry['path']} ({entry.get('size', 0)} bytes)")
        header = f"Contents of '{path or '/'}' on ref '{ref}':"
        return self._truncate(header + "\n" + "\n".join(lines))

    def read_file(
        self,
        path: str = Field(..., description="Path of the file inside the repository."),
        ref: str = Field(
            "", description="Branch, tag or commit. Empty means the configured default."
        ),
        start_line: int = Field(1, description="First line to return, 1-based."),
        end_line: int = Field(
            0, description="Last line to return. 0 means until the end of the file."
        ),
    ) -> str:
        """
        Read the contents of a file from the repository. The result is line numbered,
        so it can be referenced precisely. Use start_line and end_line to page through
        large files.
        """
        error = self._config_error()
        if error:
            return error
        path = _arg(path, "")
        start_line, end_line = int(_arg(start_line, 1)), int(_arg(end_line, 0))
        ref = self._ref(_arg(ref, ""))
        try:
            path = self._clean_path(path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not path:
            return "Error: path is required."

        try:
            blob = self._raw_file(path, ref)
        except Exception as exc:
            return self._error(exc)

        if self._is_binary(blob):
            return f"'{path}' looks like a binary file ({len(blob)} bytes) and cannot be displayed."

        note = ""
        if len(blob) > self.valves.max_file_bytes:
            blob = blob[: self.valves.max_file_bytes]
            note = f"\n[file truncated to {self.valves.max_file_bytes} bytes]"

        text = blob.decode("utf-8", errors="replace")
        lines = text.splitlines()

        start = max(1, int(start_line or 1))
        end = int(end_line or 0) or len(lines)
        end = min(end, len(lines))
        if start > len(lines):
            return f"'{path}' has only {len(lines)} lines, start_line {start} is out of range."

        body = "\n".join(
            f"{number:>6}: {line}"
            for number, line in enumerate(lines[start - 1 : end], start=start)
        )
        header = f"{path} (ref {ref}, lines {start}-{end} of {len(lines)}):"
        return self._truncate(f"{header}\n{body}{note}")

    async def grep_repo(
        self,
        pattern: str = Field(
            ..., description="Python regular expression to search for in file contents."
        ),
        path_prefix: str = Field(
            "", description="Only search files below this directory."
        ),
        file_glob: str = Field(
            "",
            description="Only search files whose name matches this glob, for example '*.py'.",
        ),
        ref: str = Field(
            "", description="Branch, tag or commit. Empty means the configured default."
        ),
        case_sensitive: bool = Field(
            False, description="Whether the search is case sensitive."
        ),
        context_lines: int = Field(
            0, description="Number of lines of context to show around each match."
        ),
        __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> str:
        """
        Search the contents of the repository files with a regular expression and
        return the matching lines with their file path and line number. Narrow the
        search with path_prefix and file_glob whenever possible, because every
        candidate file has to be downloaded.
        """
        error = self._config_error()
        if error:
            return error
        pattern, file_glob = _arg(pattern, ""), _arg(file_glob, "")
        case_sensitive = bool(_arg(case_sensitive, False))
        context_lines = int(_arg(context_lines, 0))
        ref = self._ref(_arg(ref, ""))
        if not pattern:
            return "Error: pattern is required."
        try:
            path_prefix = self._clean_path(_arg(path_prefix, ""))
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            return f"Error: invalid regular expression: {exc}"

        try:
            tree = self._tree(ref)
        except Exception as exc:
            return self._error(exc)

        prefix = f"{path_prefix}/" if path_prefix else ""
        candidates = [
            entry
            for entry in tree
            if entry["path"].startswith(prefix)
            and not self._excluded(entry["path"])
            and entry.get("size", 0) <= self.valves.max_file_bytes
            and (
                not file_glob
                or fnmatch.fnmatch(entry["path"].rsplit("/", 1)[-1], file_glob)
                or fnmatch.fnmatch(entry["path"], file_glob)
            )
        ]
        candidates.sort(key=lambda e: e["path"])

        skipped_files = 0
        if len(candidates) > self.valves.max_grep_files:
            skipped_files = len(candidates) - self.valves.max_grep_files
            candidates = candidates[: self.valves.max_grep_files]

        if not candidates:
            return f"No files to search under '{path_prefix or '/'}' on ref '{ref}'."

        await self._emit(
            __event_emitter__,
            f"Searching {len(candidates)} file(s) in {self.valves.owner}/{self.valves.repo}...",
            done=False,
        )

        def fetch(entry: dict) -> tuple[str, Optional[bytes]]:
            try:
                return entry["path"], self._raw_file(entry["path"], ref)
            except Exception:
                return entry["path"], None

        def scan() -> tuple[list[str], int, bool, int]:
            results: list[str] = []
            matches = 0
            truncated = False
            unreadable = 0

            with ThreadPoolExecutor(max_workers=max(1, self.valves.concurrency)) as pool:
                for file_path, blob in pool.map(fetch, candidates):
                    if blob is None:
                        unreadable += 1
                        continue
                    if self._is_binary(blob):
                        continue
                    lines = blob.decode("utf-8", errors="replace").splitlines()
                    for index, line in enumerate(lines):
                        if not regex.search(line):
                            continue
                        if matches >= self.valves.max_results:
                            truncated = True
                            break
                        matches += 1
                        if context_lines > 0:
                            low = max(0, index - context_lines)
                            high = min(len(lines), index + context_lines + 1)
                            results.append(
                                "\n".join(
                                    f"{file_path}:{n + 1}:{'>' if n == index else ' '} {lines[n]}"
                                    for n in range(low, high)
                                )
                            )
                        else:
                            results.append(f"{file_path}:{index + 1}: {line}")
                    if truncated:
                        break

            return results, matches, truncated, unreadable

        results, matches, truncated, unreadable = await asyncio.to_thread(scan)

        await self._emit(__event_emitter__, f"Found {matches} match(es).", done=True)

        if not results:
            body = f"No matches for '{pattern}' in {len(candidates)} file(s) on ref '{ref}'."
        else:
            separator = "\n--\n" if context_lines > 0 else "\n"
            body = (
                f"{matches} match(es) for '{pattern}' on ref '{ref}':\n"
                + separator.join(results)
            )

        notes = []
        if truncated:
            notes.append(
                f"result limit of {self.valves.max_results} matches reached, refine the search"
            )
        if skipped_files:
            notes.append(f"{skipped_files} file(s) not searched due to the file limit")
        if unreadable:
            notes.append(f"{unreadable} file(s) could not be fetched")
        if notes:
            body += "\n[" + "; ".join(notes) + "]"

        return self._truncate(body)

    @staticmethod
    async def _emit(
        emitter: Optional[Callable[[dict], Any]], message: str, done: bool
    ) -> None:
        """Best effort status update; OpenWebUI may not provide an emitter."""
        if emitter is None:
            return
        try:
            result = emitter(
                {"type": "status", "data": {"description": message, "done": done}}
            )
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass
