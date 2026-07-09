import re
import io
import zipfile
import httpx


# ---------------------------------------------------------------------------
# Binary extension set (kept local/duplicated on purpose — avoid coupling to
# backend/e2b_sandbox.py, which is out of scope for this change).
# ---------------------------------------------------------------------------
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".svg", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".wav", ".ogg", ".pdf", ".zip",
}

# Noisy directories / files we never want to import.
SKIP_DIR_PREFIXES = (
    ".git/", "node_modules/", "dist/", "build/", ".next/",
    "vendor/", "__pycache__/",
)
SKIP_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", ".DS_Store",
}


def _parse_github_repo_url(repo_url: str) -> tuple[str, str, str | None]:
    """
    Parse a GitHub repo URL into (owner, repo, branch).
    branch is None if not explicitly specified in the URL (caller should
    then try 'main' then fall back to 'master').

    Accepts:
      https://github.com/owner/repo
      https://github.com/owner/repo.git
      github.com/owner/repo
      https://github.com/owner/repo/tree/branch
    """
    if not repo_url or not repo_url.strip():
        raise ValueError("Missing GitHub repo URL.")

    url = repo_url.strip()
    # Normalize: allow missing scheme.
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    match = re.search(
        r"github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?(?:/tree/([A-Za-z0-9_.\-/]+))?/?$",
        url,
    )
    if not match:
        raise ValueError(
            "Invalid GitHub repo URL. Expected something like "
            "https://github.com/owner/repo"
        )

    owner, repo, branch = match.group(1), match.group(2), match.group(3)
    if not owner or not repo:
        raise ValueError(
            "Invalid GitHub repo URL. Could not determine owner and repo name."
        )

    return owner, repo, (branch or None)


def _is_binary_path(path: str) -> bool:
    for ext in BINARY_EXTS:
        if path.lower().endswith(ext):
            return True
    return False


def _should_skip(rel_path: str) -> bool:
    if not rel_path:
        return True
    lower = rel_path.lower()
    for prefix in SKIP_DIR_PREFIXES:
        if lower.startswith(prefix) or f"/{prefix}" in lower:
            return True
    filename = rel_path.rsplit("/", 1)[-1]
    if filename in SKIP_FILENAMES:
        return True
    if filename.endswith(".lock"):
        return True
    return False


async def fetch_github_repo_files(
    repo_url: str, max_files: int = 300, max_file_size: int = 200_000
) -> dict[str, str]:
    """
    Download a public GitHub repo as a zip (no auth required) and return a
    dict of {relative_path: text_content} for all importable text files.

    max_files is a deliberate cap (not a bug) — some repos are huge, and we
    only want a reasonable starting point for the agent to work from.
    """
    owner, repo, explicit_branch = _parse_github_repo_url(repo_url)

    branches_to_try = [explicit_branch] if explicit_branch else ["main", "master"]

    zip_bytes = None
    last_error = None
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for branch in branches_to_try:
            zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"
            try:
                resp = await client.get(zip_url)
            except Exception as e:
                last_error = str(e)
                continue

            if resp.status_code == 200:
                zip_bytes = resp.content
                break
            last_error = f"HTTP {resp.status_code} for branch '{branch}'"

    if zip_bytes is None:
        raise Exception(
            f"Repo not found or private — only public repos are supported right now: {last_error}"
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise Exception(f"Downloaded repo archive was not a valid zip file: {e}")

    files: dict[str, str] = {}
    names = zf.namelist()

    # Entries are nested under a top-level "{repo}-{branch}/" folder — strip it.
    top_level_prefix = None
    if names:
        first_slash = names[0].find("/")
        if first_slash != -1:
            top_level_prefix = names[0][: first_slash + 1]

    for name in names:
        if len(files) >= max_files:
            break

        if name.endswith("/"):
            continue  # directory entry

        rel_path = name
        if top_level_prefix and rel_path.startswith(top_level_prefix):
            rel_path = rel_path[len(top_level_prefix):]

        if not rel_path:
            continue

        if _should_skip(rel_path):
            continue

        if _is_binary_path(rel_path):
            continue

        info = zf.getinfo(name)
        if info.file_size > max_file_size:
            continue

        try:
            raw = zf.read(name)
        except Exception:
            continue

        if b"\x00" in raw[:1000]:
            continue  # looks binary

        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            continue

        files[rel_path] = text

    if not files:
        raise Exception("No importable text files found in this repo.")

    return files


async def get_github_login(access_token: str) -> str | None:
    """Return the GitHub username for a linked account's access token, or
    None if the token is missing/invalid. Used to check whether an imported
    repo belongs to the user's own connected GitHub account."""
    if not access_token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github.v3+json",
                },
            )
        if resp.status_code == 200:
            return resp.json().get("login")
    except Exception:
        pass
    return None


def repo_owner_matches(repo_url: str, github_login: str | None) -> bool:
    """True if the given GitHub login owns the repo at repo_url."""
    if not github_login:
        return False
    try:
        owner, _repo, _branch = _parse_github_repo_url(repo_url)
    except ValueError:
        return False
    return owner.strip().lower() == github_login.strip().lower()
