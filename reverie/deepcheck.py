"""reverie deepcheck: a much stricter check that really builds and test-installs the project."""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import tarfile
from pathlib import Path

from reverie import cli

SKIP_DIRS = {".git", "dist", "build", "__pycache__", ".venv", "venv", "node_modules", ".idea", ".vscode"}
TEXT_SUFFIXES = {".py", ".toml", ".md", ".txt", ".cfg", ".ini", ".json", ".yml", ".yaml", ".env", ".rst", ".html", ".js"}

# Patterns for secrets that must never be uploaded. The secret itself is never printed in full.
SECRET_PATTERNS = [
    ("a PyPI token", re.compile(r"pypi-[A-Za-z0-9_-]{30,}")),
    ("a GitHub token", re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}")),
    ("an AI or API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("an AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

PLACEHOLDERS = [
    ("the hello message in the starter code", "Hello from "),
    ("the placeholder description", "A short description of"),
    ("the placeholder author name", "Your Name"),
]


def project_files(folder):
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in files:
            yield Path(root) / name


def mask(secret):
    return secret[:6] + "..." + secret[-2:]


def part(title):
    print(f"\n--- {title}")


def check_syntax(report, folder, modules):
    part("Code can be read by Python")
    checked = 0
    broken = False
    for module in modules:
        for file in (folder / module).rglob("*.py"):
            if "__pycache__" in file.parts:
                continue
            checked += 1
            try:
                compile(file.read_text(encoding="utf-8"), str(file), "exec")
            except SyntaxError as error:
                broken = True
                report.fail(f"{file.relative_to(folder)} has a typing mistake on line {error.lineno}: {error.msg}",
                            "Fix it. Python cannot run this file.")
            except UnicodeDecodeError:
                broken = True
                report.fail(f"{file.relative_to(folder)} is not saved as normal UTF-8 text.",
                            "Re-save the file as UTF-8.")
    if not broken:
        report.ok(f"All {checked} Python file(s) are readable")


def check_secrets(report, folder):
    part("No passwords, keys or tokens inside your files")
    found = False
    scanned = 0
    for file in project_files(folder):
        if file.suffix.lower() not in TEXT_SUFFIXES and file.name not in {".env", ".gitignore"}:
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for label, pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                found = True
                report.fail(f"{file.relative_to(folder)} seems to contain {label} ({mask(match.group(0))}).",
                            "Remove it right now. Anyone who sees your package could use it. "
                            "If it was real, delete that key or token on its website and make a new one.")
    if not found:
        report.ok(f"Looked through {scanned} file(s), no keys or tokens found")


def check_placeholders(report, folder, modules, project):
    part("Starter text is replaced with your own")
    left = []
    for module in modules:
        cli_file = folder / module / "cli.py"
        if cli_file.is_file():
            text = cli_file.read_text(encoding="utf-8", errors="ignore")
            hello = f'print("Hello from {project.get("name")}!")'
            if "# This is where your own idea goes" in text and hello in text:
                left.append("the starter hello message and note in " + f"{module}/cli.py")
    description = str(project.get("description", ""))
    if description.startswith("A short description of"):
        left.append("the placeholder description in pyproject.toml")
    authors = project.get("authors") or []
    if isinstance(authors, list):
        for author in authors:
            if isinstance(author, dict) and author.get("name") == "Your Name":
                left.append("the placeholder author name 'Your Name' in pyproject.toml")
    if not authors and not project.get("authors"):
        report.warn("pyproject.toml has no author.", 'Add: authors = [{ name = "Your Name" }]')
    if left:
        for item in left:
            report.fail(f"Still here: {item}.", "Replace it with your own text before you upload.")
    else:
        report.ok("No starter text left behind")


def run(command, cwd, timeout=240):
    return subprocess.run(command, cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout, errors="replace")


def short(text, lines=6):
    rows = [r for r in text.strip().splitlines() if r.strip()]
    return "\n       ".join(rows[-lines:])


def build_copy(report, folder, temp):
    """Builds the project from a clean copy. Returns (wheel, sdist) paths or (None, None)."""
    part("Build the package from a clean copy")
    if importlib.util.find_spec("build") is None:
        report.warn("The 'build' tool is not installed, so the build test was skipped.",
                    "Install it with: pip install build twine   then run deepcheck again.")
        return None, None
    copy = temp / "copy"
    shutil.copytree(folder, copy, ignore=shutil.ignore_patterns(
        ".git", "dist", "build", "__pycache__", "*.egg-info", ".venv", "venv"))
    out = temp / "out"
    try:
        result = run([sys.executable, "-m", "build", "--outdir", str(out), str(copy)], temp, timeout=480)
    except subprocess.TimeoutExpired:
        report.fail("The build took too long and was stopped.")
        return None, None
    if result.returncode != 0:
        report.fail("The build failed. This is exactly what would go wrong when you upload.",
                    "Last lines of the build output:\n       " + short(result.stdout + "\n" + result.stderr))
        return None, None
    wheels = list(out.glob("*.whl"))
    sdists = list(out.glob("*.tar.gz"))
    if not wheels or not sdists:
        report.fail("The build finished but did not make both a .whl and a .tar.gz file.")
        return None, None
    report.ok("Built both the wheel (.whl) and the source file (.tar.gz)")
    return wheels[0], sdists[0]


def check_twine(report, wheel, sdist, temp):
    part("PyPI page and metadata test (twine check)")
    if importlib.util.find_spec("twine") is None:
        report.warn("The 'twine' tool is not installed, so this test was skipped.",
                    "Install it with: pip install build twine   then run deepcheck again.")
        return
    result = run([sys.executable, "-m", "twine", "check", str(wheel), str(sdist)], temp)
    output = result.stdout + result.stderr
    if result.returncode != 0 or "FAILED" in output:
        report.fail("twine says the package page would not work on PyPI.",
                    short(output))
    elif "WARNING" in output.upper():
        report.warn("twine passed, but gave a warning.", short(output))
    else:
        report.ok("twine says the PyPI page is fine")


def read_metadata(zf, suffix):
    for member in zf.namelist():
        if member.endswith(suffix):
            return zf.read(member).decode("utf-8", errors="replace")
    return None


def check_contents(report, wheel, sdist, modules, name, version):
    part("What is really inside the built files")
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        for module in modules:
            if f"{module}/__init__.py" not in names:
                report.fail(f"The wheel does not contain {module}/__init__.py. Your code would be missing after install.",
                            "Check the [tool.setuptools] packages line in pyproject.toml.")
            else:
                count = len([n for n in names if n.startswith(module + "/") and n.endswith(".py")])
                report.ok(f"The wheel contains your code folder '{module}' ({count} Python file(s))")
        if not any(n.endswith("/licenses/LICENSE") or n.endswith("dist-info/LICENSE") or "LICENSE" in n.split("/")[-1]
                   for n in names):
            report.fail("The wheel has no LICENSE file inside.", 'Check license-files = ["LICENSE"] in pyproject.toml.')
        else:
            report.ok("The LICENSE is inside the wheel")
        metadata = (read_metadata(zf, ".dist-info/METADATA") or "").replace("\r\n", "\n")
        meta_name = re.search(r"^Name: (.+)$", metadata, re.M)
        meta_version = re.search(r"^Version: (.+)$", metadata, re.M)
        if meta_version and version and meta_version.group(1).strip() != version:
            report.fail(f"Built version is {meta_version.group(1).strip()} but pyproject.toml says {version}.")
        elif meta_version:
            report.ok(f"Built version is {meta_version.group(1).strip()}, matching pyproject.toml")
        if meta_name and name:
            built = re.sub(r"[-_.]+", "-", meta_name.group(1).strip()).lower()
            wanted = re.sub(r"[-_.]+", "-", name).lower()
            if built != wanted:
                report.fail(f"Built name '{meta_name.group(1).strip()}' does not match '{name}'.")
        if "Description-Content-Type" not in metadata or not re.search(r"\n\n\S", metadata):
            report.warn("The built package page has no long description.",
                        "Check that readme = \"README.md\" is in pyproject.toml and the README is not empty.")
        junk = [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
        if junk:
            report.warn("The wheel contains compiled cache files (__pycache__).",
                        "They are harmless but useless. Delete the __pycache__ folders and build again.")
    with tarfile.open(sdist) as tf:
        names = [m.name.split("/", 1)[-1] for m in tf.getmembers()]
        for needed in ("pyproject.toml", "README.md"):
            if needed not in names:
                report.warn(f"The source file (.tar.gz) does not contain {needed}.")
        if not any(n.upper().startswith("LICENSE") for n in names):
            report.fail("The source file (.tar.gz) has no LICENSE.")
        else:
            report.ok("The source file (.tar.gz) has pyproject.toml, README and LICENSE")


def check_fresh_install(report, wheel, modules, project, temp):
    part("Install test in a brand-new empty environment")
    venv = temp / "venv"
    try:
        made = run([sys.executable, "-m", "venv", str(venv)], temp, timeout=240)
    except subprocess.TimeoutExpired:
        report.warn("Could not create a test environment in time, so the install test was skipped.")
        return
    if made.returncode != 0:
        report.warn("Could not create a test environment, so the install test was skipped.",
                    short(made.stdout + made.stderr))
        return
    bindir = venv / ("Scripts" if os.name == "nt" else "bin")
    python = bindir / ("python.exe" if os.name == "nt" else "python")
    try:
        install = run([str(python), "-m", "pip", "install", "--no-input", str(wheel)], temp, timeout=480)
    except subprocess.TimeoutExpired:
        report.warn("The test install took too long, so it was skipped.")
        return
    if install.returncode != 0:
        report.fail("The built package could not be installed in a clean environment.",
                    short(install.stdout + install.stderr))
        return
    report.ok("The built package installs in a clean environment")

    for module in modules:
        try:
            result = run([str(python), "-c", f"import {module}"], temp, timeout=60)
        except subprocess.TimeoutExpired:
            report.fail(f"Importing '{module}' hung and was stopped.")
            continue
        if result.returncode != 0:
            report.fail(f"After install, 'import {module}' fails.",
                        short(result.stdout + result.stderr, 2))
        else:
            report.ok(f"After install, 'import {module}' works")

    for command, target in project.get("scripts", {}).items():
        exe = bindir / (command + (".exe" if os.name == "nt" else ""))
        if not exe.exists():
            report.fail(f"The command '{command}' was not created by the install.",
                        "Check the [project.scripts] section in pyproject.toml.")
            continue
        module_path, _, function = str(target).partition(":")
        check = (f"import importlib; m = importlib.import_module({module_path!r}); "
                 f"assert callable(getattr(m, {function!r}))")
        try:
            result = run([str(python), "-c", check], temp, timeout=60)
        except subprocess.TimeoutExpired:
            report.fail(f"Checking the command '{command}' hung and was stopped.")
            continue
        if result.returncode != 0:
            report.fail(f"The command '{command}' is created, but {target} cannot be found or used.",
                        short(result.stdout + result.stderr, 2))
        else:
            report.ok(f"The command '{command}' exists after install and points to working code")
    report.info("reverie never runs your program itself, it only checks that it can be loaded.")


def command_deepcheck(args):
    folder = Path(args.folder).resolve()
    pyproject = folder / "pyproject.toml"
    print(f"Deep check of {folder}")
    print("This builds your package and installs it in a test environment, so it can take a minute.")
    if not pyproject.is_file():
        print(f"\n{cli.FAIL} No pyproject.toml found here.")
        print("       Run this inside your project folder, or give the folder: reverie deepcheck my-tool")
        return 1
    try:
        data = cli.read_pyproject(pyproject)
    except (ValueError, OSError) as error:
        print(f"\n{cli.FAIL} pyproject.toml could not be read: {error}")
        return 1

    project = data.get("project", {})
    tool = data.get("tool", {})
    name = project.get("name")
    version = str(project.get("version")) if project.get("version") else None
    modules = cli.find_modules(project, tool, name)

    report = cli.Report()
    part("Basic checks (the same ones as 'reverie check')")
    if not name:
        report.fail("pyproject.toml has no project name.")
    else:
        report.ok(f"Name: {name}")
    if not version:
        report.fail("pyproject.toml has no version number.", 'Add: version = "0.1.0"')
    elif not cli.VERSION_PATTERN.match(version):
        report.warn(f"The version '{version}' looks unusual. Normal versions look like 0.1.0")
    else:
        report.ok(f"Version: {version}")
    cli.check_readme(report, folder, project)
    cli.check_license(report, folder, project)
    cli.check_modules(report, folder, project, tool, name, version)
    if name and version:
        cli.check_dist(report, folder, name, version)
        if args.offline:
            report.info("Skipped the PyPI check (--offline).")
        else:
            cli.check_pypi(report, name, version)

    check_syntax(report, folder, modules)
    check_secrets(report, folder)
    check_placeholders(report, folder, modules, project)

    if report.fails and not args.force:
        print("\nStopping before the build tests, because the problems above should be fixed first.")
        print("(To run the build tests anyway, add --force)")
    else:
        temp = Path(tempfile.mkdtemp(prefix="reverie-deepcheck-"))
        try:
            wheel, sdist = build_copy(report, folder, temp)
            if wheel and sdist:
                check_twine(report, wheel, sdist, temp)
                check_contents(report, wheel, sdist, modules, name, version)
                if args.no_install:
                    report.info("Skipped the install test (--no-install).")
                else:
                    check_fresh_install(report, wheel, modules, project, temp)
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    print("\n" + "=" * 60)
    if report.fails:
        print(f"NOT READY: {report.fails} problem(s) and {report.warns} warning(s).")
        print("Fix every [FAIL] line above, then run 'reverie deepcheck' again.")
        print("Remember: a version on PyPI can never be changed or uploaded twice.")
        return 1
    if report.warns:
        print(f"NO PROBLEMS, but {report.warns} warning(s). Read the [WARN] lines above before you upload.")
        return 0
    print("ALL FINE. Every test passed. Your package is ready to upload.")
    return 0
