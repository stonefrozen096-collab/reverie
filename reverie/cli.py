import argparse
import json
import keyword
import re
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

NAME_PATTERN = re.compile(r"^[a-z](?:[a-z0-9-]*[a-z0-9])?$")

PYPROJECT = '''[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = @@NAME_JSON@@
version = "0.1.0"
description = @@DESCRIPTION_JSON@@
readme = "README.md"
requires-python = ">=3.9"
license = "MIT"
license-files = ["LICENSE"]
authors = [{ name = @@AUTHOR_JSON@@ }]
classifiers = [
    "Programming Language :: Python :: 3",
    "Operating System :: OS Independent",
]

# After you put your code on GitHub, remove the # signs and fix the address:
# [project.urls]
# Homepage = "https://github.com/YOUR-GITHUB-NAME/@@NAME@@"

[project.scripts]
@@NAME@@ = "@@MODULE@@.cli:main"

[tool.setuptools]
packages = ["@@MODULE@@"]
'''

README = '''# @@NAME@@

@@DESCRIPTION@@

## Install

```
pip install @@NAME@@
```

## Use

```
@@NAME@@
```

If the command is not recognised on Windows, start it through Python instead:

```
python -m @@MODULE@@
```

## License

MIT
'''

LICENSE = '''MIT License

Copyright (c) @@YEAR@@ @@AUTHOR@@

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
'''

GITIGNORE = '''# Files that should never go on GitHub
__pycache__/
*.pyc
dist/
build/
*.egg-info/
'''

INIT_PY = '''__version__ = "0.1.0"
'''

MAIN_PY = '''from @@MODULE@@.cli import main

raise SystemExit(main())
'''

CLI_PY = '''def main():
    # This is where your own idea goes. Replace these lines with your code.
    print("Hello from @@NAME@@!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def render(template, values):
    for key, value in values.items():
        template = template.replace("@@" + key + "@@", value)
    return template


def name_problem(name):
    """Returns a plain-language problem with the name, or None if it is fine."""
    if not NAME_PATTERN.match(name):
        return ("A package name can only use lowercase letters, numbers and hyphens, "
                "and must start with a letter. Example: my-tool")
    module = name.replace("-", "_")
    if keyword.iskeyword(module):
        return f"'{name}' is a special Python word and cannot be used. Pick another name."
    stdlib = getattr(sys, "stdlib_module_names", ())
    if module in stdlib:
        return f"'{name}' is the name of a built-in Python module. Pick another name."
    return None


def pypi_status(name):
    """Returns 'free', 'taken' or 'unknown' (for example when offline)."""
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        with urllib.request.urlopen(url, timeout=8):
            return "taken"
    except urllib.error.HTTPError as error:
        return "free" if error.code == 404 else "unknown"
    except (urllib.error.URLError, OSError, ValueError):
        return "unknown"


def ask(question, default):
    try:
        answer = input(f"{question} [{default}]: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def command_new(args):
    name = args.name.strip().lower()
    problem = name_problem(name)
    if problem:
        print(problem)
        return 1

    target = Path.cwd() / name
    if target.exists() and any(target.iterdir()):
        print(f"A folder called {name} already exists here and is not empty.")
        return 1

    if args.skip_check:
        print("Skipped the PyPI name check.")
    else:
        print(f"Checking if '{name}' is free on PyPI...")
        status = pypi_status(name)
        if status == "taken":
            print(f"'{name}' is already taken on PyPI. Please pick another name.")
            print("(To create the folder anyway, add --skip-check)")
            return 1
        if status == "free":
            print("Good news, the name is free.")
        else:
            print("Could not check the name right now (are you offline?). Continuing.")

    description = args.description or ask("One-line description", f"A short description of {name}.")
    author = args.author or ask("Your name", "Your Name")

    module = name.replace("-", "_")
    values = {
        "NAME": name,
        "MODULE": module,
        "NAME_JSON": json.dumps(name),
        "DESCRIPTION": description,
        "DESCRIPTION_JSON": json.dumps(description),
        "AUTHOR": author,
        "AUTHOR_JSON": json.dumps(author),
        "YEAR": str(date.today().year),
    }

    files = {
        "pyproject.toml": PYPROJECT,
        "README.md": README,
        "LICENSE": LICENSE,
        ".gitignore": GITIGNORE,
        f"{module}/__init__.py": INIT_PY,
        f"{module}/__main__.py": MAIN_PY,
        f"{module}/cli.py": CLI_PY,
    }
    for relative, template in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(template, values), encoding="utf-8")

    print(f"\nCreated the project folder: {target}")
    for relative in files:
        print(f"  {relative}")
    print("\nNext steps:")
    print(f"  1. cd {name}")
    print("  2. pip install -e .")
    print(f"  3. {name}        (it prints a hello message)")
    print(f"  4. Open {module}\\cli.py and replace the hello message with your own idea")
    print("  5. reverie check      (looks for problems before you upload)")
    return 0


# ---------------------------------------------------------------- reverie check

OK, INFO, WARN, FAIL = "[ OK ]", "[INFO]", "[WARN]", "[FAIL]"
VERSION_PATTERN = re.compile(r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")


class Report:
    def __init__(self):
        self.fails = 0
        self.warns = 0

    def ok(self, message):
        print(f"{OK} {message}")

    def info(self, message):
        print(f"{INFO} {message}")

    def warn(self, message, fix=None):
        self.warns += 1
        print(f"{WARN} {message}")
        if fix:
            print(f"       {fix}")

    def fail(self, message, fix=None):
        self.fails += 1
        print(f"{FAIL} {message}")
        if fix:
            print(f"       {fix}")


def simple_toml(text):
    """A tiny reader for older Python versions that have no built-in TOML reader."""
    data = {"project": {"scripts": {}}, "tool": {"setuptools": {}}}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line.strip("[]").strip()
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*=\s*(.+)$", line)
        if not match:
            continue
        key, value = match.groups()
        quoted = re.match(r'^"([^"]*)"', value)
        if section == "project" and key in ("name", "version", "readme") and quoted:
            data["project"][key] = quoted.group(1)
        elif section == "project.scripts" and quoted:
            data["project"]["scripts"][key] = quoted.group(1)
        elif section == "tool.setuptools" and key == "packages":
            data["tool"]["setuptools"]["packages"] = re.findall(r'"([^"]+)"', value)
    return data


def read_pyproject(path):
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib
    except ImportError:
        return simple_toml(text)
    return tomllib.loads(text)


def find_modules(project, tool, name):
    packages = tool.get("setuptools", {}).get("packages")
    if isinstance(packages, list) and packages:
        return [str(p) for p in packages]
    modules = []
    for target in project.get("scripts", {}).values():
        root = str(target).split(":")[0].split(".")[0]
        if root and root not in modules:
            modules.append(root)
    if not modules and name:
        modules.append(name.replace("-", "_"))
    return modules


def check_readme(report, folder, project):
    readme = project.get("readme")
    if isinstance(readme, dict):
        readme = readme.get("file")
    if not readme:
        report.warn("No README is set in pyproject.toml, so your PyPI page will be empty.",
                    'Add: readme = "README.md"')
        return
    path = folder / readme
    if not path.is_file():
        report.fail(f"pyproject.toml points to {readme}, but that file does not exist.",
                    f"Create {readme}, or the build will fail.")
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        report.warn(f"{readme} is empty, so your PyPI page will be empty.")
        return

    problems = False
    escaped = 0
    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if not in_code and re.search(r"\\[#*_\[\]!]", line):
            escaped += 1
    if escaped >= 3:
        problems = True
        report.fail(f"{readme} has {escaped} lines with a backslash in front of symbols like # or **.",
                    "On PyPI your headings and bold text would show as plain symbols. Re-create the file.")
    elif escaped:
        report.warn(f"{readme} has a backslash in front of a symbol on {escaped} line(s). Is that on purpose?")

    numeric = len(re.findall(r"&#x?[0-9A-Fa-f]+;", text))
    named = len(re.findall(r"&(?:gt|lt|amp|quot);", text))
    if numeric:
        problems = True
        report.fail(f"{readme} contains {numeric} code(s) like &#x20; where normal characters should be.",
                    "This happens when text is pasted from a web page. Replace them with the real characters.")
    if named:
        report.warn(f"{readme} contains {named} code(s) like &gt; or &amp;. Is that on purpose?")

    if len(re.findall(r"\n{4,}", text)) >= 3:
        problems = True
        report.warn(f"{readme} has many extra blank lines, which can break lists and tables.")

    for target in re.findall(r"!\[[^\]]*\]\(([^)\s]+)", text):
        if not target.lower().startswith(("http://", "https://")):
            problems = True
            report.warn(f"The image {target} in {readme} will not show on PyPI.",
                        "PyPI cannot read files from your computer. Upload the picture to GitHub "
                        "and use its full https:// address.")
    if not problems:
        report.ok(f"{readme} looks clean")


def check_modules(report, folder, project, tool, name, version):
    for module in find_modules(project, tool, name):
        module_dir = folder / module
        if not (module_dir / "__init__.py").is_file():
            report.fail(f"The code folder '{module}' is missing or has no __init__.py file.",
                        "Without it your code will not be included in the package.")
            continue
        report.ok(f"Code folder '{module}' found")
        if not (module_dir / "__main__.py").is_file():
            report.warn(f"'{module}' has no __main__.py file, so 'python -m {module}' will not work.",
                        "Add it: people whose command is 'not recognised' can still start your tool this way.")
        else:
            report.ok("python -m works (found __main__.py)")
        init_text = (module_dir / "__init__.py").read_text(encoding="utf-8", errors="replace")
        match = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", init_text)
        if match and version and match.group(1) != version:
            report.warn(f"The version in {module}/__init__.py ({match.group(1)}) differs from "
                        f"pyproject.toml ({version}).", "Update both so they match.")
        if list(module_dir.rglob("__pycache__")):
            report.info("Found __pycache__ folders. Leave them out when you upload to GitHub.")

    for command, target in project.get("scripts", {}).items():
        module_path, _, function = str(target).partition(":")
        relative = Path(*module_path.split("."))
        file = folder / relative.with_suffix(".py")
        if not file.is_file():
            file = folder / relative / "__init__.py"
        if not file.is_file():
            report.fail(f"The command '{command}' points to {target}, but that file does not exist.",
                        "Typing the command after install would fail.")
            continue
        source = file.read_text(encoding="utf-8", errors="replace")
        if function and not re.search(r"def\s+" + re.escape(function) + r"\b", source):
            report.fail(f"The command '{command}' needs a function called {function}() "
                        f"in {file.name}, but it was not found.")
        else:
            report.ok(f"The command '{command}' points to real code")


def check_license(report, folder, project):
    declared = project.get("license-files") or []
    missing = [f for f in declared if not (folder / f).exists()]
    if missing:
        report.fail(f"pyproject.toml lists {', '.join(missing)}, but the file does not exist.",
                    "The build would fail.")
    elif (folder / "LICENSE").is_file() or declared:
        report.ok("LICENSE found")
    else:
        report.warn("No LICENSE file. Many people will not use a package without a license.")


def check_dist(report, folder, name, version):
    dist = folder / "dist"
    if not dist.is_dir():
        report.info("Not built yet. When ready, run: python -m build")
        return
    files = [f.name for f in dist.iterdir()
             if f.name.endswith((".tar.gz", ".whl", ".zip"))]
    if not files:
        report.info("The dist folder is empty. Run: python -m build")
        return
    pattern = re.compile(r"-" + re.escape(version) + r"(\.tar\.gz|-[^/\\]*\.whl|\.zip)$")
    stale = [f for f in files if not pattern.search(f)]
    if stale:
        report.fail(f"The dist folder has old files that are not version {version}: {', '.join(stale)}",
                    "Upload would try to send them too. Delete the folder first (Windows: rmdir /s /q dist) "
                    "and build again.")
    else:
        report.ok(f"dist has files for version {version}")


def check_pypi(report, name, version):
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            report.ok(f"'{name}' is not on PyPI yet, so this will be your first upload")
        else:
            report.info("Could not check PyPI right now.")
        return
    except (urllib.error.URLError, OSError, ValueError):
        report.info("Could not check PyPI right now (are you offline?).")
        return
    latest = data.get("info", {}).get("version", "?")
    if version in data.get("releases", {}):
        report.fail(f"Version {version} is already on PyPI, and a version can never be uploaded twice.",
                    "Raise the version number in pyproject.toml (for example to the next one) and build again.")
    else:
        report.ok(f"Version {version} is new on PyPI (latest there: {latest})")
        report.info(f"A project called '{name}' already exists on PyPI. "
                    "If it is yours, you are fine. If not, pick another name.")


def command_check(args):
    folder = Path(args.folder).resolve()
    pyproject = folder / "pyproject.toml"
    print(f"Checking {folder}\n")
    if not pyproject.is_file():
        print(f"{FAIL} No pyproject.toml found here.")
        print("       Run this command inside your project folder, or give the folder: reverie check my-tool")
        return 1
    try:
        data = read_pyproject(pyproject)
    except (ValueError, OSError) as error:
        print(f"{FAIL} pyproject.toml could not be read: {error}")
        return 1

    report = Report()
    project = data.get("project", {})
    tool = data.get("tool", {})
    name = project.get("name")
    version = project.get("version")

    if not name:
        report.fail("pyproject.toml has no project name.")
    else:
        report.ok(f"Name: {name}")
    if not version:
        report.fail("pyproject.toml has no version number.", 'Add: version = "0.1.0"')
    elif not VERSION_PATTERN.match(str(version)):
        report.warn(f"The version '{version}' looks unusual. Normal versions look like 0.1.0")
    else:
        report.ok(f"Version: {version}")

    check_readme(report, folder, project)
    check_license(report, folder, project)
    check_modules(report, folder, project, tool, name, version)
    if name and version:
        check_dist(report, folder, name, str(version))
        if args.offline:
            report.info("Skipped the PyPI check (--offline).")
        else:
            check_pypi(report, name, str(version))

    print()
    if report.fails:
        print(f"Result: {report.fails} problem(s) and {report.warns} warning(s). "
              "Fix the [FAIL] lines before you upload.")
        return 1
    if report.warns:
        print(f"Result: no problems, but {report.warns} warning(s). Have a look at the [WARN] lines.")
    else:
        print("Result: no problems found.")
    print("\nWhen you are ready to upload:")
    print("  1. Delete the old dist folder, if there is one")
    print("  2. python -m build")
    print("  3. twine check dist/*")
    print("  4. twine upload dist/*")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="reverie",
        description="Turn your idea into a published Python package.",
    )
    commands = parser.add_subparsers(dest="command")

    new = commands.add_parser("new", help="Create a new package project folder")
    new.add_argument("name", help="Name of your package, for example: my-tool")
    new.add_argument("--description", help="One-line description")
    new.add_argument("--author", help="Your name")
    new.add_argument("--skip-check", action="store_true", help="Do not check the name on PyPI")

    check = commands.add_parser("check", help="Look for problems in a project before you upload it")
    check.add_argument("folder", nargs="?", default=".", help="Project folder (default: this folder)")
    check.add_argument("--offline", action="store_true", help="Do not look at PyPI")

    deep = commands.add_parser(
        "deepcheck",
        help="A very thorough check: builds and test-installs your package before you upload")
    deep.add_argument("folder", nargs="?", default=".", help="Project folder (default: this folder)")
    deep.add_argument("--offline", action="store_true", help="Do not look at PyPI")
    deep.add_argument("--no-install", action="store_true", help="Skip the clean install test")
    deep.add_argument("--force", action="store_true",
                      help="Run the build tests even if the basic checks found problems")

    publish = commands.add_parser(
        "publish",
        help="Run the deep check, build, and upload your package to PyPI")
    publish.add_argument("folder", nargs="?", default=".", help="Project folder (default: this folder)")
    publish.add_argument("--test", action="store_true",
                         help="Upload to TestPyPI, the practice website, instead of the real PyPI")
    publish.add_argument("--dry-run", action="store_true",
                         help="Do everything except the upload")

    github = commands.add_parser(
        "github",
        help="Upload your project to GitHub using a Personal Access Token")
    github.add_argument("folder", nargs="?", default=".", help="Project folder (default: this folder)")
    github.add_argument("--repo", help="Repository to upload to, like your-name/my-tool")
    github.add_argument("--private", action="store_true",
                        help="If the repository has to be created, make it private")
    github.add_argument("--message", help="The note GitHub shows next to the uploaded files")
    github.add_argument("--dry-run", action="store_true",
                        help="Show what would be uploaded, without uploading")

    args = parser.parse_args()
    if args.command == "new":
        return command_new(args)
    if args.command == "check":
        return command_check(args)
    if args.command == "github":
        from reverie.github import command_github
        return command_github(args)
    if args.command == "publish":
        from reverie.publish import command_publish
        return command_publish(args)
    if args.command == "deepcheck":
        from reverie.deepcheck import command_deepcheck
        return command_deepcheck(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
