"""reverie publish: checks, builds and uploads your package to PyPI."""
import getpass
import os
import re
import shutil
import subprocess
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

from reverie import cli
from reverie import deepcheck

PYPI_URL = "https://upload.pypi.org/legacy/"
TESTPYPI_URL = "https://test.pypi.org/legacy/"


def scrub(text, secret):
    """Makes sure the token can never appear on screen."""
    if secret:
        text = text.replace(secret, "[hidden]")
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    rows = [r for r in text.splitlines() if "kB" not in r and "━" not in r]
    return re.sub(r"pypi-[A-Za-z0-9_-]{20,}", "[hidden]", "\n".join(rows))


def explain_upload_error(output, test):
    """Turns twine's technical output into a plain-language explanation."""
    low = output.lower()
    site = "TestPyPI" if test else "PyPI"
    if "already exists" in low or "file already exists" in low or "400 bad request" in low and "exist" in low:
        return ("This version is already on " + site + ", and a version can never be uploaded twice.\n"
                "       Raise the version number in pyproject.toml (for example 0.2.1), then run reverie publish again.")
    if "403" in low or "invalid or non-existent authentication" in low or "forbidden" in low:
        extra = ("\n       TestPyPI is a separate website from PyPI, so you need a separate account and token there."
                 if test else "")
        return ("The website refused the token (403 Forbidden). The usual reasons:\n"
                "       - The token was copied wrongly (it must be the whole thing, starting with pypi-)\n"
                "       - The token was made for a different project, or has been deleted\n"
                "       - For the very first upload of a new name, the token must be 'Entire account', not one project"
                + extra)
    if "401" in low or "unauthorized" in low:
        return "The token was not accepted (401). Check that you pasted the whole token."
    if any(word in low for word in ("connection", "dns", "name resolution", "timed out", "timeout", "max retries")):
        return ("Could not reach " + site + ". This looks like an internet problem, not a problem with your package.\n"
                "       Check your connection and run reverie publish again. Nothing was uploaded.")
    if "invalid distribution" in low or "invalid" in low and "metadata" in low:
        return "The website says the package files are not valid. Run reverie deepcheck and read it carefully."
    return "The upload did not finish. The last lines of what twine said are shown above."


def command_publish(args):
    folder = Path(args.folder).resolve()
    test = args.test
    site = "TestPyPI (practice site)" if test else "PyPI (the real site)"
    pyproject = folder / "pyproject.toml"

    print(f"Publishing {folder}")
    print(f"Target: {site}\n")
    if not pyproject.is_file():
        print(f"{cli.FAIL} No pyproject.toml found here.")
        print("       Run this inside your project folder, or give the folder: reverie publish my-tool")
        return 1
    try:
        data = cli.read_pyproject(pyproject)
    except (ValueError, OSError) as error:
        print(f"{cli.FAIL} pyproject.toml could not be read: {error}")
        return 1
    project = data.get("project", {})
    name = project.get("name")
    version = str(project.get("version") or "")
    if not name or not version:
        print(f"{cli.FAIL} pyproject.toml needs both a name and a version.")
        return 1

    # Step 1: the deep check must pass first.
    print("Step 1 of 3: running the deep check first...")
    check_args = Namespace(folder=str(folder), offline=test, no_install=False, force=False)
    if deepcheck.command_deepcheck(check_args) != 0:
        print("\nPublishing stopped. Nothing was uploaded.")
        print("Fix the problems above and run reverie publish again.")
        return 1

    # Step 2: build clean files in a temporary place, so old files can never be sent by accident.
    print("\n\nStep 2 of 3: building the files to upload...")
    temp = Path(tempfile.mkdtemp(prefix="reverie-publish-"))
    try:
        report = cli.Report()
        wheel, sdist = deepcheck.build_copy(report, folder, temp)
        if not (wheel and sdist):
            print("\nThe build did not work, so nothing was uploaded.")
            return 1
        files = [wheel, sdist]

        print("\nReady to upload:")
        for file in files:
            print(f"  {file.name}")
        print(f"\n  Package : {name}")
        print(f"  Version : {version}")
        print(f"  Website : {site}")

        if args.dry_run:
            print("\nDry run: stopping here. Nothing was uploaded.")
            return 0

        # Step 3: confirm, then upload.
        print("\nStep 3 of 3: upload")
        if not test:
            print("This is the REAL PyPI. After this, this version can never be changed or uploaded again.")
        try:
            typed = input(f"To continue, type the version number ({version}) and press Enter, or just press Enter to cancel: ").strip()
        except EOFError:
            typed = ""
        if typed != version:
            print("Cancelled. Nothing was uploaded.")
            return 1

        token = os.environ.get("REVERIE_PYPI_TOKEN", "").strip()
        if not token:
            print("\nPaste your PyPI token. Nothing will show on screen while you paste. This is normal.")
            print("(On Windows, a right-click pastes. Then press Enter.)")
            try:
                token = getpass.getpass("Token: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nCancelled. Nothing was uploaded.")
                return 1
        if not token.startswith("pypi-"):
            print(f"{cli.FAIL} That does not look like a token. A token starts with pypi- and is very long.")
            print("       Nothing was uploaded.")
            return 1

        env = dict(os.environ)
        env["TWINE_USERNAME"] = "__token__"
        env["TWINE_PASSWORD"] = token
        env.pop("REVERIE_PYPI_TOKEN", None)
        command = [sys.executable, "-m", "twine", "upload", "--non-interactive",
                   "--repository-url", TESTPYPI_URL if test else PYPI_URL] + [str(f) for f in files]
        print("\nUploading...")
        try:
            result = subprocess.run(command, cwd=str(temp), env=env, capture_output=True,
                                    text=True, timeout=600, errors="replace")
        except subprocess.TimeoutExpired:
            print(f"{cli.FAIL} The upload took too long and was stopped. Check your internet and try again.")
            return 1
        except FileNotFoundError:
            print(f"{cli.FAIL} twine is not installed. Run: pip install twine")
            return 1
        output = scrub(result.stdout + "\n" + result.stderr, token)
        token = None
        env = None

        if result.returncode != 0:
            rows = [r for r in output.strip().splitlines() if r.strip()]
            print("\n".join(rows[-8:]))
            print(f"\n{cli.FAIL} {explain_upload_error(output, test)}")
            return 1

        host = "test.pypi.org" if test else "pypi.org"
        print(f"\n{cli.OK} Uploaded!")
        print(f"\nYour package page: https://{host}/project/{name}/{version}/")
        print("It can take a minute or two to appear.")
        print("\nTo try it, in a new window:")
        if test:
            print(f"  pip install --index-url https://test.pypi.org/simple/ --no-deps {name}")
        else:
            print(f"  pip install --upgrade {name}")
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)
