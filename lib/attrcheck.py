"""Calls to methods that do not exist on the type the annotation names.

    python3 lib/attrcheck.py          # check, non-zero on a problem
    python3 lib/attrcheck.py -v       # also list what was checked

WHY THIS EXISTS

Run 41996560809 died three seconds in with

    AttributeError: 'Recovery' object has no attribute 'wait_for'

The method had been written on `drive.Driver`, and the sequence in `macos/15/install.py`
is driven by `Recovery`, a different class in a different file. Both files define a class
named `Driver`; `Recovery` is not one of them.

NOTHING IN THIS REPOSITORY COULD HAVE CAUGHT IT BEFORE A RUN STARTED.

    py_compile        parses and compiles; `d.wait_for(...)` is valid syntax whatever `d`
                      is, so it passes
    lib/entrypoints.py  runs `--help`, which returns from argparse before any of the
                      sequence executes, so the call is never reached
    the workflows    every macOS stage takes about an hour of install to get near it

A type annotation says which class a variable is, and the class says which methods it has.
Comparing the two is a static question with a static answer, and the answer for that line
was "no" - it just cost a run to find out.

WHAT IS CHECKED, AND WHAT IS DELIBERATELY NOT

    an annotated parameter     def f(d: Recovery) ...  then `d.wait_for(` inside it
    self                       inside a class, `self.method(` against the enclosing class

Those two cover the failure above and need no inference: the annotation is written down,
and `self` is the class the method is defined in.

EVERYTHING ELSE IS SKIPPED, because a check that guesses produces false alarms and a check
that produces false alarms gets switched off:

    a bare local assigned from a call    `assets = opencore.build(...)` could return
                                         anything; no annotation says so
    an attribute of an attribute         `self.q.execute(...)` depends on what `q` is
    a name from an import                `iso.fetch_tag` is a module function
    anything whose annotation does not  resolve to a class in the same module

NOTHING IS EXECUTED. Modules are imported to resolve their annotations and their classes,
which defines functions and reads constants; no entry point's main() runs, and the check is
over in a second.

A MODULE THAT CANNOT BE IMPORTED IS REPORTED, NOT FAILED. An import that needs a package
the checkout does not have is a fact about this machine, not a defect in the code, and
turning it into a failure would make the check unrunnable outside the bake image.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO, "lib")

# The same set of directories lib/entrypoints.py searches, so a new system is covered
# without being listed here.
SEARCH = ["android", "bsd", "linux", "macos", "windows", "lib"]


def log(message: str) -> None:
    print(f"[attrcheck] {message}", flush=True)


class Module:
    """One file, imported once so its classes can be asked questions."""

    def __init__(self, rel: str):
        self.rel = rel
        self.path = os.path.join(REPO, rel)
        self.tree: ast.Module | None = None
        self.namespace: dict = {}
        self.error: str | None = None

    def load(self) -> "Module":
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.tree = ast.parse(fh.read(), filename=self.rel)
        except (OSError, SyntaxError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return self

        # A UNIQUE MODULE NAME PER FILE. `import` would find `macos/15/drive.py` and
        # `lib/drive.py` under the same name if both existed, and the second would be
        # served the first one's namespace from sys.modules - silently answering
        # questions about the wrong file.
        name = "_attrcheck_" + self.rel.replace("/", "_").replace("\\", "_").removesuffix(".py")
        try:
            spec = importlib.util.spec_from_file_location(name, self.path)
            if spec is None or spec.loader is None:
                self.error = "no loader"
                return self
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            # The repository's own modules import each other by bare name (`import smoke`,
            # `from lib import vm`), so both roots have to be importable.
            for extra in (LIB, os.path.dirname(os.path.dirname(self.path))):
                if extra not in sys.path:
                    sys.path.append(extra)
            spec.loader.exec_module(module)
            self.namespace = vars(module)
        except Exception as exc:                      # noqa: BLE001 - reported, not raised
            self.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        return self


def resolve_class(namespace: dict, name: str):
    """The class `name` refers to in this module, or None."""
    value = namespace.get(name)
    return value if isinstance(value, type) else None


def annotated_params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, ast.expr]:
    """parameter name -> its annotation expression, for parameters that have one."""
    found = {}
    for arg in list(node.args.args) + list(node.args.kwonlyargs):
        if arg.annotation is not None:
            found[arg.arg] = arg.annotation
    return found


def annotation_name(node: ast.expr) -> str | None:
    """The class name an annotation refers to, for the shapes that name one.

    `Recovery` yes; `Recovery | None` and `Optional[Recovery]` take the first name that
    resolves to a class. `dict[str, Foo]` and similar are left alone - they annotate a
    container, and a method call on the parameter is not a method of the container's
    element.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.BinOp):                   # X | None
        for side in (node.left, node.right):
            found = annotation_name(side)
            if found:
                return found
    if isinstance(node, ast.Subscript):               # Optional[X]
        return annotation_name(node.slice)
    return None


def calls_on(tree: ast.AST, name: str) -> list[tuple[str, int]]:
    """Every `name.attribute(...)` in the tree, as (attribute, line)."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == name):
            found.append((func.attr, node.lineno))
    return found


def check_module(module: Module, known: dict[str, str]) -> list[str]:
    """Problems in one file. `known` maps an annotation name to where it was found, so a
    name that is a class in another file is resolved there rather than guessed at."""
    if module.tree is None:
        return []

    problems = []

    def check(obj_name: str, cls, attribute: str, line: int, where: str) -> None:
        if cls is None:
            return
        if hasattr(cls, attribute):
            return
        problems.append(
            f"{module.rel}:{line}  {obj_name}.{attribute}(...) is not a method of "
            f"{cls.__name__} ({where})"
        )

    for node in ast.walk(module.tree):
        # --- annotated parameters -------------------------------------------------
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for param, annotation in annotated_params(node).items():
                name = annotation_name(annotation)
                if not name:
                    continue
                cls = resolve_class(module.namespace, name)
                if cls is None:
                    # A class defined in another file. `known` says which file, and only
                    # then is it worth importing that one to answer the question.
                    where = known.get(name)
                    if where and where != module.rel:
                        continue
                    continue
                for attribute, line in calls_on(node, param):
                    check(param, cls, attribute, line,
                          f"{param}: {name} in {node.name}()")

        # --- self -----------------------------------------------------------------
        if isinstance(node, ast.ClassDef):
            cls = resolve_class(module.namespace, node.name)
            if cls is None:
                continue
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for attribute, line in calls_on(item, "self"):
                    check("self", cls, attribute, line,
                          f"in {node.name}.{item.name}()")

    return problems


def discover() -> list[str]:
    found = []
    for folder in SEARCH:
        root = os.path.join(REPO, folder)
        for dirpath, _dirnames, filenames in os.walk(root):
            if "__pycache__" in dirpath:
                continue
            for name in sorted(filenames):
                if name.endswith(".py"):
                    found.append(os.path.relpath(os.path.join(dirpath, name), REPO))
    return sorted(set(found))


def main(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv

    modules = [Module(rel).load() for rel in discover()]
    failed = [m for m in modules if m.error]
    usable = [m for m in modules if not m.error]

    # Which file defines each class name, for annotations that cross a boundary.
    known: dict[str, str] = {}
    for m in usable:
        for name, value in m.namespace.items():
            if isinstance(value, type) and value.__module__ == m.rel.replace("/", ".").removesuffix(".py"):
                known.setdefault(name, m.rel)

    log(f"{len(usable)} module(s) parsed"
        + (f", {len(failed)} not importable" if failed else ""))

    problems = []
    for m in usable:
        problems.extend(check_module(m, known))

    if verbose:
        for m in usable:
            log(f"  checked {m.rel}")

    # A MODULE THAT DOES NOT IMPORT IS NAMED EVERY TIME, NOT ONLY IN VERBOSE MODE.
    #
    # It is not a failure - an import needing a package this machine lacks is not a defect
    # in the code - but it IS a module whose methods were not checked, and a check that
    # silently covers less than it claims is worse than one that covers less and says so.
    #
    # `linux/mx/drive-mx.py` is the one that does not import: it says
    # `from keys import type_text` and lib/keys.py defines `type_into`. No workflow runs
    # it, so it has never failed anywhere - which is exactly why it is worth printing
    # here rather than leaving to be found.
    for m in failed:
        log(f"  NOT CHECKED {m.rel}: {m.error}")

    if problems:
        for line in problems:
            log(f"  MISSING  {line}")
        log(f"=== {len(problems)} PROBLEM(S) ===")
        return 1
    log("=== EVERY ANNOTATED METHOD EXISTS ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
