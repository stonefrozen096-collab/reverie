"""reverie write: an AI company's model writes the whole package from your idea."""
import ast
import contextlib
import getpass
import io
import json
import os
import re
import sys
from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path

from reverie import cli
from reverie import providers

PLAN_TOKENS = 1500
FILE_TOKENS = 4096
README_TOKENS = 1500
MAX_FILES = 6
MAX_FIX_TRIES = 2
RISKY_CALLS = {"eval", "exec", "os.system", "shutil.rmtree", "os.remove", "os.unlink", "os.rmdir",
               "os.removedirs", "subprocess.call", "subprocess.run", "subprocess.Popen", "subprocess.check_output"}
DEPENDENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,_-]+\])?([<>=!~]=?[0-9A-Za-z.*]+)?$")
FILE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,30}\.py$")

SYSTEM = (
    "You are an expert Python developer. You write small, clean, working command-line Python packages "
    "for beginners. Rules: the code must work on Python 3.9 or newer; keep it simple and readable with "
    "short comments; use argparse for the command line; never print or store passwords or keys; never "
    "delete or change files unless the idea clearly requires it, and then ask the user to confirm first; "
    "do not make internet calls unless the idea clearly requires it. Follow the reply format you are given "
    "exactly, with no extra talk."
)


# ---------------------------------------------------------------- usage log

def usage_file():
    home = os.environ.get("REVERIE_HOME")
    base = Path(home) if home else Path.home() / ".reverie"
    return base / "usage.json"


def load_usage():
    path = usage_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_usage_entry(entry):
    path = usage_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = load_usage()
        data.append(entry)
        path.write_text(json.dumps(data[-500:], indent=1), encoding="utf-8")
        return True
    except OSError:
        return False


def command_usage(args):
    data = load_usage()
    if not data:
        print("No usage yet. Tokens are counted each time you run: reverie write")
        return 0
    today = datetime.now().date()
    print("Tokens used by reverie write (counted on this computer only)\n")
    print("Last 7 days:")
    for back in range(6, -1, -1):
        day = today - timedelta(days=back)
        rows = [e for e in data if e.get("date") == day.isoformat()]
        tin = sum(e.get("tokens_in", 0) for e in rows)
        tout = sum(e.get("tokens_out", 0) for e in rows)
        label = "today" if back == 0 else day.isoformat()
        print(f"  {label:<12} {len(rows)} package(s)   {tin + tout:>9,} tokens   (in {tin:,}, out {tout:,})")
    print("\nLatest packages:")
    for entry in data[-10:][::-1]:
        total = entry.get("tokens_in", 0) + entry.get("tokens_out", 0)
        print(f"  {entry.get('date', '?')}  {entry.get('package', '?'):<24} {total:>9,} tokens   "
              f"{entry.get('calls', 0)} call(s)   {entry.get('provider', '')} / {entry.get('model', '')}")
    print("\nTo work out your spending, look up your company's price for these tokens.")
    print("Prices change, so reverie does not guess them. 'in' is what you sent, 'out' is what came back.")
    return 0


# ---------------------------------------------------------------- helpers

def ask(question, default=""):
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def slugify(text):
    words = re.findall(r"[a-z0-9]+", text.lower())
    stop = {"a", "an", "the", "for", "to", "of", "that", "which", "and", "my", "tool", "package", "python"}
    words = [w for w in words if w not in stop][:3]
    slug = "-".join(words) or "my-tool"
    return slug if slug[0].isalpha() else "tool-" + slug


def extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON found")
    return json.loads(text[start:end + 1])


def extract_code(text):
    blocks = re.findall(r"```[a-zA-Z0-9_-]*\n(.*?)```", text, re.S)
    code = max(blocks, key=len) if blocks else text
    return code.strip("\n") + "\n"


def defined_names(source):
    names = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


class Run:
    """Counts every call so the token total is always right."""

    def __init__(self, provider, model):
        self.provider, self.model = provider, model
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def chat(self, prompt, max_tokens):
        text, tin, tout = self.provider.chat(self.model, SYSTEM, prompt, max_tokens)
        self.calls += 1
        self.tokens_in += tin
        self.tokens_out += tout
        if not text.strip():
            raise providers.AIError(0, "The model sent back an empty answer")
        return text


# ---------------------------------------------------------------- auto checks

def check_python_file(source, filename, module, planned, written, allow_deps, deps):
    """Returns (problems, warnings) for one generated file."""
    problems, warnings = [], []
    try:
        tree = ast.parse(source, filename)
        compile(source, filename, "exec")
    except SyntaxError as error:
        return [f"typing mistake on line {error.lineno}: {error.msg}"], warnings
    stdlib = getattr(sys, "stdlib_module_names", None)
    allowed_third = {re.split(r"[\[<>=!~]", d)[0].replace("-", "_").lower() for d in deps}
    for node in ast.walk(tree):
        imported = []
        if isinstance(node, ast.Import):
            imported = [(a.name, None, 0) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported = [(node.module or "", [a.name for a in node.names], node.level)]
        for name, names, level in imported:
            root = name.split(".")[0]
            if level and level > 0:
                target = name.split(".")[0] if name else None
                if target and f"{target}.py" not in planned and target not in ("__init__",):
                    problems.append(f"imports '{target}', which is not one of the planned files")
                continue
            if root == module:
                sub = name.split(".")[1] if "." in name else None
                if sub and f"{sub}.py" not in planned and sub not in ("cli",):
                    problems.append(f"imports {module}.{sub}, which is not one of the planned files")
                elif sub and names and sub + ".py" in written:
                    missing = [n for n in names if n not in defined_names(written[sub + ".py"]) and n != "*"]
                    if missing:
                        problems.append(f"imports {', '.join(missing)} from {sub}.py, but {sub}.py does not define it")
                continue
            if stdlib is not None and root and root not in stdlib and root.lower() not in allowed_third:
                problems.append(f"uses '{root}', which is not part of standard Python"
                                + ("" if allow_deps else " (you chose no extra pip packages)"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call = dotted(node.func)
            if call in RISKY_CALLS:
                warnings.append(f"{filename} line {node.lineno}: calls {call}(). Read this part carefully.")
            for keyword in node.keywords:
                if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                    warnings.append(f"{filename} line {node.lineno}: runs a command with shell=True. Read this part carefully.")
    if filename == "cli.py":
        if "main" not in defined_names(source):
            problems.append("has no main() function, which the command needs")
    return problems, warnings


# ---------------------------------------------------------------- prompts

def plan_prompt(idea, extras, module, allow_deps):
    deps = ("You may list a few well-known pip packages in 'dependencies'." if allow_deps
            else "Use only the standard Python library. 'dependencies' must be an empty list.")
    return (
        f"Plan a small Python package called '{module}'.\n\nIdea: {idea}\n"
        f"Extra wishes: {extras or 'none'}\n\n"
        f"Rules: list between 1 and {MAX_FILES} Python files that go inside the package folder. "
        "File names are lowercase with underscores and end in .py. Do not list __init__.py or __main__.py, they "
        "already exist. One file must be cli.py: it holds a main() function that reads the command line and calls "
        f"the other files. List cli.py last. {deps}\n\n"
        'Reply with ONLY JSON in this shape: {"files": [{"path": "core.py", "purpose": "one short sentence"}, '
        '{"path": "cli.py", "purpose": "..."}], "dependencies": []}'
    )


def file_prompt(idea, extras, module, plan, path, purpose, written, allow_deps):
    listing = "\n".join(f"- {f['path']}: {f['purpose']}" for f in plan)
    earlier = "\n\n".join(f"FILE {name}:\n```python\n{code[:6000]}\n```" for name, code in written.items())
    extra = ("" if allow_deps else "Use only the standard Python library. ")
    cli_rule = (f"This is the command-line file. It must define main() that returns an int, parse arguments with "
                f"argparse, and end with: if __name__ == \"__main__\": raise SystemExit(main()). "
                if path == "cli.py" else "")
    return (
        f"We are writing the Python package '{module}'.\nIdea: {idea}\nExtra wishes: {extras or 'none'}\n\n"
        f"All files in the package folder:\n{listing}\n\n"
        f"{'Files already written:' + chr(10) + earlier + chr(10) + chr(10) if earlier else ''}"
        f"Now write the file {path}. Its job: {purpose}\n"
        f"{extra}{cli_rule}To import another file of this package, write: from {module}.<file_name_without_py> import <name>. "
        "Only import names that exist in the files already written above. "
        "Reply with ONLY the complete file in one ```python code block."
    )


def fix_prompt(path, source, problems):
    return (
        f"The file {path} has these problems:\n- " + "\n- ".join(problems) +
        f"\n\nHere is the file:\n```python\n{source}\n```\n\n"
        "Reply with ONLY the complete corrected file in one ```python code block."
    )


def readme_prompt(idea, module, name, files):
    code = "\n\n".join(f"FILE {n}:\n```python\n{c[:5000]}\n```" for n, c in files.items())
    return (
        f"Write the 'Use' section of the README for the command '{name}' (Python module '{module}').\n"
        f"Idea: {idea}\n\n{code}\n\n"
        "Rules: use only options and commands that really exist in the code above. Show 2 to 4 short examples in "
        "```text code blocks, each followed by one plain sentence. Maximum 25 lines. Do not write a heading, do not "
        "write install steps. Reply with ONLY the markdown."
    )


# ---------------------------------------------------------------- the command

def pick_provider(args):
    options = providers.PROVIDERS
    if args.provider:
        for cls in options:
            if cls.key == args.provider.lower():
                return cls
        print(f"{cli.FAIL} Unknown company '{args.provider}'. Choose one of: " + ", ".join(c.key for c in options))
        return None
    print("Which AI company's key do you want to use?")
    for number, cls in enumerate(options, 1):
        print(f"  {number}. {cls.name}")
    answer = ask("Type the number", "1")
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1]
    for cls in options:
        if answer.lower() == cls.key:
            return cls
    print(f"{cli.FAIL} That is not one of the choices.")
    return None


def command_write(args):
    print("reverie write: an AI company's model writes your whole package from your idea.")
    print("It uses YOUR key, so the tokens are spent from YOUR account. Nothing is saved except a count of tokens.\n")

    cls = pick_provider(args)
    if cls is None:
        return 1
    print(f"\n{cls.key_help}")
    key = os.environ.get("REVERIE_AI_KEY", "").strip()
    if not key:
        print("\nPaste your key. Nothing will show on screen while you paste. This is normal.")
        print("(On Windows, a right-click pastes. Then press Enter.)")
        try:
            key = getpass.getpass("Key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
    if not key:
        print("Cancelled.")
        return 1
    provider = cls(key)

    print("\nChecking your key...")
    try:
        models = provider.list_models()
    except providers.AIError as error:
        print(f"{cli.FAIL} Key is NOT valid or could not be checked.")
        if error.status == 404:
            print("       The company's address did not answer as expected (404). This is a problem in reverie, not your key.")
        else:
            print(f"       {providers.explain(error)}")
        return 1
    if not models:
        print(f"{cli.FAIL} The key works, but no usable models were found for it.")
        return 1
    print(f"{cli.OK} Key is valid. {len(models)} model(s) available to this key.")

    model = args.model
    if not model:
        shown = models[:20]
        print("\nWhich model should write your package?")
        for number, name in enumerate(shown, 1):
            print(f"  {number}. {name}")
        if len(models) > len(shown):
            print(f"  (and {len(models) - len(shown)} more. You can type any model name instead of a number.)")
        answer = ask("Type the number", "1")
        if answer.isdigit() and 1 <= int(answer) <= len(shown):
            model = shown[int(answer) - 1]
        elif answer in models:
            model = answer
        else:
            print(f"{cli.FAIL} That is not one of the models.")
            return 1
    print(f"Using: {model}\n")

    # Questions
    print("A few questions about your package:\n")
    idea = ""
    while not idea:
        idea = ask("1. What is this package for? Describe your idea in a sentence or two")
        if not idea and sys.stdin.isatty() is False:
            print("Cancelled.")
            return 1
    default_name = slugify(idea)
    name = ""
    while True:
        name = ask("2. What should the package be called", default_name).strip().lower()
        problem = cli.name_problem(name)
        if problem:
            print("   " + problem)
            if sys.stdin.isatty() is False:
                return 1
            continue
        if not args.offline:
            status = cli.pypi_status(name)
            if status == "taken":
                print(f"   '{name}' is already taken on PyPI. Pick another name.")
                if sys.stdin.isatty() is False:
                    return 1
                continue
            print("   Good news, the name is free." if status == "free" else "   Could not check the name right now. Continuing.")
        break
    first = re.split(r"(?<=[.!?])\s", idea)[0][:100]
    description = ask("3. One-line description for PyPI", first.rstrip("."))
    author = ask("4. Your name", "Your Name")
    extras = ask("5. Anything specific? Commands it should have, things to avoid. Press Enter to skip")
    allow = ask("6. Allow other pip packages (not only standard Python)? Type yes or no", "no").lower().startswith("y")

    module = name.replace("-", "_")
    target = Path.cwd() / name
    if target.exists() and any(target.iterdir()):
        print(f"\n{cli.FAIL} A folder called {name} already exists here and is not empty.")
        return 1

    print(f"\nSummary\n  Package : {name}\n  Company : {cls.name}\n  Model   : {model}\n  Idea    : {idea}")
    print("\nreverie will send your answers to " + cls.name + ". About 8 to 12 calls will be made in total.")
    if ask("Type yes to start, or just press Enter to cancel").lower() != "yes":
        print("Cancelled. Nothing was sent.")
        return 1

    run = Run(provider, model)
    result = _write_package(run, name, module, idea, extras, description, author, allow, target, args)
    entry = {"date": datetime.now().date().isoformat(), "time": datetime.now().strftime("%H:%M"),
             "package": name, "provider": cls.key, "model": model, "calls": run.calls,
             "tokens_in": run.tokens_in, "tokens_out": run.tokens_out}
    if run.calls:
        save_usage_entry(entry)
    print(f"\nTokens used for this package: {run.tokens_in + run.tokens_out:,} "
          f"(in {run.tokens_in:,}, out {run.tokens_out:,}) over {run.calls} call(s).")
    print("See your totals any time with: reverie usage")
    return result


def _fail_ai(error, target_made):
    print(f"\n{cli.FAIL} {providers.explain(error)}")
    if target_made:
        print("       The files written so far are kept in the folder. Nothing was uploaded anywhere.")
    return 1


def _write_package(run, name, module, idea, extras, description, author, allow, target, args):
    # Step 1: plan
    print("\nStep 1: planning the files...")
    plan_data = None
    for attempt in range(2):
        try:
            reply = run.chat(plan_prompt(idea, extras, module, allow), PLAN_TOKENS)
            plan_data = extract_json(reply)
            break
        except providers.AIError as error:
            return _fail_ai(error, False)
        except ValueError:
            if attempt == 1:
                print(f"{cli.FAIL} The model did not send a usable plan. Nothing was written. Try again, or pick another model.")
                return 1
    plan = []
    for item in plan_data.get("files", []):
        path = str(item.get("path", "")).strip().replace("\\", "/").split("/")[-1]
        if (FILE_NAME_PATTERN.match(path) and path not in ("__init__.py", "__main__.py")
                and all(p["path"] != path for p in plan)):
            plan.append({"path": path, "purpose": str(item.get("purpose", "")).strip()[:200] or "part of the package"})
    plan = [p for p in plan if p["path"] != "cli.py"][:MAX_FILES - 1]
    cli_item = next((i for i in plan_data.get("files", []) if str(i.get("path", "")).endswith("cli.py")), {})
    plan.append({"path": "cli.py", "purpose": str(cli_item.get("purpose", "")).strip()[:200]
                 or "the command line: reads the arguments and calls the other files"})
    deps = []
    if allow:
        for dep in plan_data.get("dependencies", []) or []:
            if isinstance(dep, str) and DEPENDENCY_PATTERN.match(dep.strip()):
                deps.append(dep.strip())
        deps = deps[:6]

    print("\nThe plan:")
    for item in plan:
        print(f"  {item['path']:<18} {item['purpose']}")
    if deps:
        print("  Extra pip packages: " + ", ".join(deps))
    if ask(f"\nType yes to write these {len(plan)} file(s), or just press Enter to stop here").lower() != "yes":
        print("Stopped. No files were written.")
        return 1

    # Step 2: scaffold with the same safe templates as 'reverie new'
    print("\nStep 2: creating the project folder...")
    quiet = io.StringIO()
    with contextlib.redirect_stdout(quiet):
        code = cli.command_new(Namespace(name=name, description=description, author=author, skip_check=True))
    if code != 0:
        print(f"{cli.FAIL} Could not create the project folder.\n" + quiet.getvalue())
        return 1
    print(f"{cli.OK} Created {target}")
    module_dir = target / module

    # Step 3: write every file, checking each one
    print("\nStep 3: writing the files, checking each one as we go...")
    planned = {p["path"] for p in plan}
    written, all_warnings = {}, []
    for number, item in enumerate(plan, 1):
        path = item["path"]
        try:
            source = extract_code(run.chat(
                file_prompt(idea, extras, module, plan, path, item["purpose"], written, allow), FILE_TOKENS))
            problems, warnings = check_python_file(source, path, module, planned, written, allow, deps)
            tries = 0
            while problems and tries < MAX_FIX_TRIES:
                tries += 1
                print(f"[{number}/{len(plan)}] {path}: found a problem ({problems[0]}). Asking the model to fix it ({tries}/{MAX_FIX_TRIES})...")
                source = extract_code(run.chat(fix_prompt(path, source, problems), FILE_TOKENS))
                problems, warnings = check_python_file(source, path, module, planned, written, allow, deps)
        except providers.AIError as error:
            return _fail_ai(error, True)
        if problems:
            print(f"{cli.FAIL} {path} still has problems after {MAX_FIX_TRIES} fixes:")
            for problem in problems:
                print("       - " + problem)
            print("       Stopped. The files written so far are kept. Try again, or pick another model.")
            return 1
        (module_dir / path).write_text(source, encoding="utf-8")
        written[path] = source
        all_warnings += warnings
        print(f"[{number}/{len(plan)}] {path}  {cli.OK} checked")

    unused = [n for n in written if n != "cli.py" and not re.search(
        r"(from\s+" + re.escape(module) + r"\.|from\s+\.|import\s+" + re.escape(module) + r"\.)" + re.escape(n[:-3]),
        written.get("cli.py", ""))]
    for n in unused:
        all_warnings.append(f"cli.py never uses {n}, so the command may not do what you asked.")

    # Step 4: dependencies and README
    if deps:
        pyproject = target / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        text = text.replace('requires-python = ">=3.9"',
                            'requires-python = ">=3.9"\ndependencies = ' + json.dumps(deps), 1)
        pyproject.write_text(text, encoding="utf-8")
    print("\nStep 4: writing the README usage section...")
    try:
        usage = run.chat(readme_prompt(idea, module, name, written), README_TOKENS).strip()
        usage = re.sub(r"^#+ .*\n+", "", usage).replace("&#x20;", " ")
        if usage.count("```") % 2 == 0 and usage:
            readme = target / "README.md"
            text = readme.read_text(encoding="utf-8")
            start, end = text.find("## Use"), text.find("## License")
            if start != -1 and end > start:
                tail = ("If the command is not recognised on Windows, start it through Python instead:\n\n"
                        f"```\npython -m {module}\n```\n\n")
                readme.write_text(text[:start] + "## Use\n\n" + usage + "\n\n" + tail + text[end:], encoding="utf-8")
                print(f"{cli.OK} README updated")
        else:
            print(f"{cli.WARN} The README text from the model looked broken, so the simple starter README was kept.")
    except providers.AIError as error:
        print(f"{cli.WARN} Could not write the README usage section ({providers.explain(error)}). The simple starter README was kept.")

    # Step 5: the same basic check as 'reverie check'
    print("\nStep 5: final check of the new project\n")
    shown = io.StringIO()
    with contextlib.redirect_stdout(shown):
        cli.command_check(Namespace(folder=str(target), offline=True))
    for line in shown.getvalue().splitlines():
        print(line)
        if line.startswith("Result:"):
            break

    print("\n" + "=" * 60)
    print(f"Your package '{name}' is finished and saved in:\n  {target}")
    if all_warnings:
        print("\nRead these parts of the code carefully before you use it:")
        for warning in all_warnings:
            print("  - " + warning)
    print("\nImportant: the code was written by an AI. reverie only checked that it can be read and that its")
    print("pieces fit together. It was never run. Read it, and try it yourself.")
    print("\nNext steps:")
    print(f"  1. cd {name}")
    print("  2. pip install -e .")
    print(f"  3. {name} --help      (try your new command)")
    print("  4. reverie deepcheck  (a very thorough check)")
    print("  5. reverie publish    (upload to PyPI)")
    return 0
