# openwebui-forgejo-tool

OpenWebUI tool that lets an LLM explore a single Forgejo repository: list files,
read files and grep file contents through the [Forgejo API](https://git.tuerantuer.org/api/swagger).

## Installation

1. In OpenWebUI go to **Workspace → Tools → +** (new tool).
2. Paste the contents of [`forgejo_tool.py`](forgejo_tool.py) and save.
3. Open the tool's **Valves** and configure at least `base_url`, `token`,
   `owner` and `repo`.
4. Enable the tool for the models or chats that should use it.

## Token

Create the token under **Settings → Applications** in Forgejo. The scope
`read:repository` is sufficient; the tool only performs GET requests. The token
is shared by every user of the tool, so it should only have access to
repositories that all of those users are allowed to read.

## Valves

| Valve | Default | Description |
| --- | --- | --- |
| `base_url` | `https://git.tuerantuer.org` | Forgejo instance, no trailing slash |
| `token` | — | API token, sent as `Authorization: token …` |
| `owner` | — | Owner of the repository |
| `repo` | — | Repository name |
| `default_ref` | *(empty)* | Ref used when the model gives none; empty resolves the repository default branch |
| `max_file_bytes` | `200000` | Larger files are skipped by grep and truncated by read |
| `max_grep_files` | `2000` | Upper bound of files fetched per grep |
| `max_results` | `100` | Upper bound of grep matches returned |
| `max_output_chars` | `40000` | Hard cap on the size of any tool result |
| `exclude_paths` | see source | Comma separated globs never listed or searched |
| `concurrency` | `8` | Parallel file downloads during grep |
| `timeout` | `30` | HTTP timeout in seconds |

The repository is fixed by the valves. `owner` and `repo` are deliberately not
tool arguments, so the model cannot reach any other repository.

## Tools exposed to the model

| Tool | Purpose |
| --- | --- |
| `get_repo_info()` | Repository metadata and the effective default ref |
| `list_branches()` | Branch names, to pick a valid `ref` |
| `list_files(path, ref, recursive)` | Directory listing, optionally the whole subtree |
| `read_file(path, ref, start_line, end_line)` | Line numbered file contents, with paging |
| `grep_repo(pattern, path_prefix, file_glob, ref, case_sensitive, context_lines)` | Regex search over file contents |

## How grep works, and what it costs

The Forgejo API has **no code content search endpoint** — `/repos/search` only
matches repository names and descriptions. `grep_repo` therefore searches
client side:

1. fetch the recursive git tree of the ref (cached per ref for the lifetime of
   the tool instance),
2. filter candidates by `path_prefix`, `file_glob`, `exclude_paths` and file size,
3. download the remaining files in parallel via the raw endpoint,
4. match the regex locally.

That means one API request per candidate file. Narrow searches with
`path_prefix` and `file_glob` whenever possible; `max_grep_files` is the safety
net against accidentally downloading an entire large repository.

Binary files are detected by a NUL byte in the first kilobyte and skipped.
Whenever results are cut short — by `max_results`, `max_grep_files`,
`max_file_bytes` or `max_output_chars` — the tool says so in its output, so the
model knows the answer is partial.

## License

MIT, see [LICENSE](LICENSE).
