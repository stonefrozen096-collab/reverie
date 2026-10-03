"""reverie github: uploads your project to GitHub using a Personal Access Token."""
import base64
import getpass
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from reverie import cli
from reverie import deepcheck

API = "https://api.github.com"
SKIP_DIRS = {".git", "dist", "build", "__pycache__", ".venv", "venv", ".idea", ".vscode", "node_modules"}
SKIP_SUFFIXES = (".pyc", ".pyo")
SKIP_NAMES = {".env", ".DS_Store", "Thumbs.db"}
MAX_BYTES = 25 * 1024 * 1024

TOKEN_HELP = (
    "How to make a token (once):\n"
    "  1. On GitHub click your picture > Settings > Developer settings\n"
    "  2. Personal access tokens > Tokens (classic) > Generate new token (classic)\n"
    "  3. Give it a name, tick the box called 'repo', and create it\n"
    "  4. Copy the token (it starts with ghp_) and keep it private"
)


class GitHubError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def scrub(text, secret):
    if secret:
        text = text.replace(secret, "[hidden]")
    return re.sub(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}", "[hidden]", text)


class Client:
    def __init__(self, token):
        self.token = token
        self.base = os.environ.get("REVERIE_GITHUB_API", API).rstrip("/")

    def call(self, method, path, body=None, retries=2):
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "reverie",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(retries + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read().decode("utf-8")
                    return response.status, (json.loads(raw) if raw else {})
            except urllib.error.HTTPError as error:
                raw = error.read().decode("utf-8", errors="replace")
                try:
                    message = json.loads(raw).get("message", raw)
                except ValueError:
                    message = raw
                if error.code >= 500 and attempt < retries:
                    time.sleep(2)
                    continue
                raise GitHubError(error.code, scrub(str(message), self.token))
            except (urllib.error.URLError, OSError) as error:
                if attempt < retries:
                    time.sleep(2)
                    continue
                raise GitHubError(0, "Could not reach GitHub (" + scrub(str(error), self.token) + ")")


def git_blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def collect_files(folder):
    files = []
    skipped = []
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(names):
            path = Path(root) / name
            if name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES):
                continue
            if path.stat().st_size > MAX_BYTES:
                skipped.append(path.relative_to(folder).as_posix())
                continue
            files.append(path)
    return files, skipped


def guess_repo(folder, pyproject_text, name):
    match = re.search(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", pyproject_text)
    if match:
        return match.group(1), match.group(2).removesuffix(".git")
    return None, name


def explain(error, what):
    status = error.status
    if status == 0:
        return error.message + ". Check your internet and try again."
    if status == 401:
        return ("GitHub did not accept the token (401). Check that you pasted the whole token, "
                "and that it has not expired or been deleted.")
    if status == 403 and "rate limit" in error.message.lower():
        return "GitHub says you are going too fast (rate limit). Wait a few minutes and run the command again."
    if status in (403, 404):
        return (f"GitHub would not let this token {what} ({status}). The usual reasons:\n"
                "       - The token does not have the 'repo' box ticked\n"
                "       - The repository belongs to someone else or is spelled differently\n"
                "       - For a token that only covers chosen repositories, this one was not chosen")
    if status == 409:
        return "GitHub says the file changed while uploading (409). Run the command again."
    if status == 422:
        return f"GitHub refused: {error.message}"
    return f"GitHub said: {error.message} ({status})"


def command_github(args):
    folder = Path(args.folder).resolve()
    pyproject = folder / "pyproject.toml"
    print(f"Uploading {folder} to GitHub\n")
    if not pyproject.is_file():
        print(f"{cli.FAIL} No pyproject.toml found here.")
        print("       Run this inside your project folder, or give the folder: reverie github my-tool")
        return 1
    text = pyproject.read_text(encoding="utf-8", errors="replace")
    try:
        data = cli.read_pyproject(pyproject)
    except (ValueError, OSError) as error:
        print(f"{cli.FAIL} pyproject.toml could not be read: {error}")
        return 1
    project = data.get("project", {})
    name = project.get("name") or folder.name
    version = str(project.get("version") or "")

    files, skipped = collect_files(folder)
    print(f"Found {len(files)} file(s) to upload.")
    print("(Left out on purpose: dist, build, __pycache__, .egg-info, .env and similar.)")
    for path in skipped:
        print(f"{cli.WARN} Skipped {path} because it is bigger than 25 MB.")

    # Never put passwords or keys on GitHub.
    report = cli.Report()
    deepcheck.check_secrets(report, folder)
    if report.fails:
        print("\nUpload stopped. Nothing was uploaded. Remove the secret first, then run this again.")
        return 1

    owner_hint, repo_default = guess_repo(folder, text, name)
    if args.repo:
        if "/" in args.repo:
            owner_hint, repo_default = args.repo.split("/", 1)
        else:
            repo_default = args.repo

    if args.dry_run:
        print("\nFiles that would be uploaded:")
        for path in files:
            print("  " + path.relative_to(folder).as_posix())
        target = f"{owner_hint}/{repo_default}" if owner_hint else repo_default
        print(f"\nTarget repository: {target}")
        print("Dry run: stopping here. Nothing was uploaded.")
        return 0

    token = os.environ.get("REVERIE_GITHUB_TOKEN", "").strip()
    if not token:
        print("\nPaste your GitHub token. Nothing will show on screen while you paste. This is normal.")
        print("(On Windows, a right-click pastes. Then press Enter.)")
        print("Do not have one? " + TOKEN_HELP.replace("\n", "\n  "))
        try:
            token = getpass.getpass("Token: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled. Nothing was uploaded.")
            return 1
    if not token:
        print("Cancelled. Nothing was uploaded.")
        return 1

    client = Client(token)
    try:
        _, user = client.call("GET", "/user")
    except GitHubError as error:
        print(f"\n{cli.FAIL} {explain(error, 'sign in')}")
        return 1
    login = user.get("login", "")
    print(f"\n{cli.OK} Signed in to GitHub as {login}")

    owner = owner_hint or login
    repo = repo_default
    if owner.lower() != login.lower():
        print(f"{cli.WARN} The project points to {owner}/{repo}, but this token belongs to {login}.")
        print("       You can only upload to your own repositories, or ones you were given access to.")

    created = False
    try:
        client.call("GET", f"/repos/{owner}/{repo}")
        print(f"{cli.OK} Repository found: {owner}/{repo}")
    except GitHubError as error:
        if error.status != 404:
            print(f"\n{cli.FAIL} {explain(error, 'look at that repository')}")
            return 1
        if owner.lower() != login.lower():
            print(f"\n{cli.FAIL} {owner}/{repo} was not found, and a repository can only be created under your own name.")
            return 1
        print(f"{cli.INFO} The repository {owner}/{repo} does not exist yet.")
        kind = "private" if args.private else "public"
        try:
            answer = input(f"Create it now as a {kind} repository? Type yes to create, or press Enter to cancel: ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "yes":
            print("Cancelled. Nothing was uploaded.")
            return 1
        try:
            client.call("POST", "/user/repos", {
                "name": repo, "private": bool(args.private),
                "description": str(project.get("description") or "")[:300],
            })
        except GitHubError as error2:
            print(f"\n{cli.FAIL} {explain(error2, 'create a repository')}")
            return 1
        created = True
        print(f"{cli.OK} Created {owner}/{repo}")

    print(f"\nReady to upload {len(files)} file(s) to https://github.com/{owner}/{repo}")
    try:
        answer = input(f"To continue, type the repository name ({repo}) and press Enter, or just press Enter to cancel: ").strip()
    except EOFError:
        answer = ""
    if answer != repo:
        print("Cancelled. Nothing was uploaded.")
        return 1

    message = args.message or (f"Add {name} {version}" if version else f"Add {name}")
    new = updated = same = 0
    total = len(files)
    for number, path in enumerate(files, 1):
        relative = path.relative_to(folder).as_posix()
        content = path.read_bytes()
        api_path = f"/repos/{owner}/{repo}/contents/" + urllib.parse.quote(relative)
        sha = None
        try:
            _, existing = client.call("GET", api_path)
            if isinstance(existing, dict):
                sha = existing.get("sha")
        except GitHubError as error:
            if error.status != 404:
                print(f"\n{cli.FAIL} {explain(error, 'read ' + relative)}")
                return 1
        if sha and sha == git_blob_sha(content):
            same += 1
            print(f"[{number}/{total}] {relative}  (already the same)")
            continue
        body = {"message": message, "content": base64.b64encode(content).decode("ascii")}
        if sha:
            body["sha"] = sha
        try:
            client.call("PUT", api_path, body)
        except GitHubError as error:
            print(f"\n{cli.FAIL} Stopped at {relative}: {explain(error, 'upload files here')}")
            print(f"       {new + updated} file(s) were uploaded before it stopped. Run the command again to continue.")
            return 1
        if sha:
            updated += 1
            print(f"[{number}/{total}] {relative}  (updated)")
        else:
            new += 1
            print(f"[{number}/{total}] {relative}  (new)")
        time.sleep(0.3)

    print(f"\n{cli.OK} Done: {new} new, {updated} updated, {same} already the same.")
    print(f"Your repository: https://github.com/{owner}/{repo}")

    if version:
        tag = "v" + version
        try:
            notes = input(f"\nWant a release called {tag}? Type a short description and press Enter, or just press Enter to skip: ").strip()
        except EOFError:
            notes = ""
        if notes:
            try:
                client.call("POST", f"/repos/{owner}/{repo}/releases", {
                    "tag_name": tag, "name": f"{tag}", "body": notes})
                print(f"{cli.OK} Release created: https://github.com/{owner}/{repo}/releases/tag/{tag}")
            except GitHubError as error:
                if error.status == 422:
                    print(f"{cli.WARN} Could not create the release. A release called {tag} may already exist.")
                else:
                    print(f"{cli.WARN} {explain(error, 'create a release')}")
    return 0
