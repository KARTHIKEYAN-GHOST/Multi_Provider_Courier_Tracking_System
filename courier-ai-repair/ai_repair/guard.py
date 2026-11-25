"""Decides whether code proposed by the AI model may enter the repository.

The model's answer is untrusted: the provider answer it was shown may have contained text
written to mislead it. Nothing here asks the model for anything; these are fixed rules,
applied to the proposed files before a single one of them is written or executed.

Rules, in short:
  * only the adapter's own files and its one test file may be proposed;
  * only an approved list of modules may be imported;
  * nothing that executes text, opens files, starts programs or reaches into Python's
    internals may be used;
  * every web address written in the code must belong to the provider's approved hosts;
  * environment variables may be read only if they are named after the adapter.

This is defence in depth, not a proof: see docs/phase-9.md for what it does not catch.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

ADAPTERS = "courier-adapters/adapters"
TESTS = "courier-adapters/tests"
MAX_FILES = 8
MAX_FILE_CHARS = 60_000
_KEY = re.compile(r"^[a-z][a-z0-9]{0,29}$")
_MODULE_FILE = re.compile(r"^[a-z_][a-z0-9_]{0,40}\.py$")
_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://([^/\s\"\'<>?#]*)")

_ADAPTER_IMPORTS = frozenset(
    {"__future__", "os", "re", "typing", "datetime", "json", "dataclasses", "httpx", "bs4",
     "courier_adapter_sdk", "courier_adapter_sdk.http", "courier_adapter_sdk.contracts",
     "courier_adapter_sdk.credentials", "adapters.india", "adapters.tables"}
)  # fmt: skip
_TEST_IMPORTS = _ADAPTER_IMPORTS | {"pytest", "base64", "pathlib", "urllib.parse", "tests.adapter_support"}
_BANNED_NAMES = frozenset(
    {"eval", "exec", "compile", "__import__", "open", "globals", "locals", "vars", "getattr", "setattr",
     "delattr", "breakpoint", "input", "memoryview", "__builtins__", "__loader__", "__spec__"}
)  # fmt: skip
_BANNED_ATTRIBUTES = frozenset(
    {"write_text", "write_bytes", "unlink", "rmdir", "rmtree", "chmod", "symlink_to", "hardlink_to",
     "touch", "mkdir", "rename", "system", "popen", "spawn", "fork", "kill", "putenv", "environb",
     "open", "walk", "glob", "rglob", "iterdir", "home", "expanduser"}
)  # fmt: skip
#: Hosts a test may name besides the provider's own: reserved names that lead nowhere.
_TEST_ONLY_HOSTS = ("example", "example.com", "example.org", "example.net", "test", "invalid", "localhost")


class Rejected(Exception):
    """The proposal breaks a rule. The text says which, and is safe to show the model."""


@dataclass(frozen=True)
class ProposedFile:
    path: str
    content: str


def valid_key(key: str) -> bool:
    return bool(_KEY.match(key))


def kind_of(path: str, key: str) -> str | None:
    """``"adapter"``, ``"test"`` or ``None`` when the model may not write this path."""
    if path == f"{TESTS}/test_{key}_adapter.py":
        return "test"
    prefix = f"{ADAPTERS}/{key}/"
    if path.startswith(prefix) and _MODULE_FILE.match(path[len(prefix) :]):
        return "adapter"
    return None


def hosts_in(source: str) -> set[str]:
    """Hosts of the web addresses written in a piece of code."""
    return {host.rsplit("@", 1)[-1].split(":")[0].lower() for host in _URL.findall(source)} - {""}


def _module_allowed(module: str, allowed: frozenset[str], key: str, kind: str) -> bool:
    if kind == "test" and (module == f"adapters.{key}" or module.startswith(f"adapters.{key}.")):
        return True
    # Exact names only: allowing a package must not allow everything inside it.
    return module in allowed


def _host_allowed(host: str, allowed: set[str], kind: str) -> bool:
    if host in allowed:
        return True
    return kind == "test" and (host in _TEST_ONLY_HOSTS or host.endswith((".example", ".test", ".invalid")))


def check_python(file: ProposedFile, key: str, allowed_hosts: set[str]) -> None:
    """Raises ``Rejected`` unless the file keeps every rule."""
    kind = kind_of(file.path, key)
    if kind is None:
        raise Rejected(f"{file.path}: only the adapter's own files and its test file may be written")
    if len(file.content) > MAX_FILE_CHARS:
        raise Rejected(f"{file.path}: larger than {MAX_FILE_CHARS} characters")
    try:
        tree = ast.parse(file.content)
    except SyntaxError as error:
        raise Rejected(f"{file.path}: not valid Python (line {error.lineno})") from None
    imports = _TEST_IMPORTS if kind == "test" else _ADAPTER_IMPORTS
    env_prefix = key.upper() + "_"

    # The only permitted use of the name "os" is as the start of "os.environ".
    os_in_environ = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.attr == "environ"
    }

    for node in ast.walk(tree):
        where = f"{file.path}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _module_allowed(alias.name, imports, key, kind):
                    raise Rejected(f"{where}: module '{alias.name}' may not be imported")
                if alias.name == "os" and alias.asname:
                    raise Rejected(f"{where}: 'os' may not be imported under another name")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if not _module_allowed(node.module or "", imports, key, kind):
                    raise Rejected(f"{where}: module '{node.module}' may not be imported")
                if node.module == "os":
                    raise Rejected(f"{where}: write 'import os'; nothing may be imported from it")
            elif kind == "adapter":
                # ``.parser`` is the adapter's own package; ``..india`` / ``..tables`` are shared.
                shared = node.module in ("india", "tables") if node.level == 2 else node.level == 1
                if not shared:
                    raise Rejected(f"{where}: only the adapter's own modules, ..india and ..tables")
            elif node.level != 1 or node.module != "adapter_support":
                raise Rejected(f"{where}: a test may import .adapter_support only, besides the adapter")
        elif isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            raise Rejected(f"{where}: '{node.id}' may not be used")
        elif isinstance(node, ast.Name) and node.id == "os" and id(node) not in os_in_environ:
            raise Rejected(f"{where}: of 'os' only os.environ.get may be used")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr in _BANNED_ATTRIBUTES:
                raise Rejected(f"{where}: '.{node.attr}' may not be used")
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            text = node.value if isinstance(node.value, str) else node.value.decode("latin-1")
            for host in hosts_in(text):
                if not _host_allowed(host, allowed_hosts, kind):
                    raise Rejected(f"{where}: the address of host '{host}' is not an approved host")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise Rejected(f"{where}: global and nonlocal may not be used")

    # os.environ may only be read, and only variables named after this adapter.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            ok = False
            for parent in ast.walk(tree):
                if (
                    isinstance(parent, ast.Call)
                    and isinstance(parent.func, ast.Attribute)
                    and parent.func.attr == "get"
                    and parent.func.value is node
                    and parent.args
                    and isinstance(parent.args[0], ast.Constant)
                    and isinstance(parent.args[0].value, str)
                    and parent.args[0].value.startswith(env_prefix)
                ):
                    ok = True
            if not ok:
                raise Rejected(
                    f"{file.path}:{node.lineno}: environment variables may only be read with "
                    f"os.environ.get('{env_prefix}...')"
                )


def _class_constant(source: str, name: str) -> str | None:
    """The text assigned to ``name`` in a class body of the module, e.g. ``version``."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                target: ast.expr | None = None
                if isinstance(item, ast.AnnAssign):
                    target = item.target
                elif isinstance(item, ast.Assign) and len(item.targets) == 1:
                    target = item.targets[0]
                value = getattr(item, "value", None)
                if (
                    isinstance(target, ast.Name)
                    and target.id == name
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    return value.value
    return None


def adapter_version(source: str) -> str | None:
    return _class_constant(source, "version")


def test_names(source: str) -> set[str]:
    return {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    }


def check_proposal(
    files: list[ProposedFile],
    key: str,
    allowed_hosts: set[str],
    *,
    before: dict[str, str],
    fixtures: list[str],
) -> None:
    """Checks a whole proposal. ``before`` holds the current content of the adapter's files
    (empty for a new adapter); ``fixtures`` are file names the test file must use."""
    if not valid_key(key):
        raise Rejected("the adapter key is not valid")
    if not files:
        raise Rejected("no files were proposed")
    if len(files) > MAX_FILES:
        raise Rejected(f"more than {MAX_FILES} files were proposed")
    paths = [file.path for file in files]
    if len(set(paths)) != len(paths):
        raise Rejected("a file was proposed twice")
    for file in files:
        check_python(file, key, allowed_hosts)

    after = {**before, **{file.path: file.content for file in files}}
    adapter_path, test_path = f"{ADAPTERS}/{key}/adapter.py", f"{TESTS}/test_{key}_adapter.py"
    for needed in (adapter_path, f"{ADAPTERS}/{key}/__init__.py", f"{ADAPTERS}/{key}/__main__.py", test_path):
        if needed not in after:
            raise Rejected(f"{needed} is missing")
    adapter = after[adapter_path]
    if _class_constant(adapter, "key") != key:
        raise Rejected(f'{adapter_path}: the adapter class must have key = "{key}"')
    version = adapter_version(adapter)
    if version is None or not re.match(r"^\d+\.\d+\.\d+$", version):
        raise Rejected(f"{adapter_path}: the adapter class must have a version like 0.1.0")
    if adapter_path in before and adapter_version(before[adapter_path]) == version:
        raise Rejected(f"{adapter_path}: the version must be raised (it is still {version})")
    if "keep_evidence(" not in adapter:
        raise Rejected(f"{adapter_path}: call ctx.keep_evidence(response) before reading the answer")

    tests = after[test_path]
    if len(test_names(tests)) < max(3, len(test_names(before.get(test_path, "")))):
        raise Rejected(f"{test_path}: has fewer tests than before (or fewer than three)")
    for name in fixtures:
        if name not in tests:
            raise Rejected(f"{test_path}: must test the adapter against the fixture file {name}")
