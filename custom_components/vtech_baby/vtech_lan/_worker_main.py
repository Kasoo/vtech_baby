"""Start the worker process: `python -I _worker_main.py <package> --fd N [...]` (see worker.py).

Run by path, never imported. `<package>` is the dotted name Home Assistant imported this integration
under (e.g. `custom_components.vtech_baby`). Its parent packages are registered here as empty modules
pointing at their directories, so `<package>.vtech_lan` imports without running the integration's
`__init__.py`, which imports Home Assistant. The worker then holds no Home Assistant code, and
`vtech_lan` classes keep the same names in both processes (so they cross the IPC intact).

The integration's own directory must never be on sys.path: its platform modules are named by Home
Assistant (select.py, sensor.py, ...) and would shadow modules of the same name. `-I` keeps this
script's directory off it; the check below catches anything else.
"""
import importlib
import importlib.machinery
import os
import sys
import types


def _register_parents(package: str, package_dir: str) -> None:
    names = package.split(".")
    d = package_dir
    for i in range(len(names), 0, -1):
        name = ".".join(names[:i])
        mod = types.ModuleType(name)
        mod.__spec__ = importlib.machinery.ModuleSpec(name, None, is_package=True)
        mod.__spec__.submodule_search_locations = mod.__path__ = [d]
        sys.modules[name] = mod
        d = os.path.dirname(d)


def _start() -> None:
    package = sys.argv[1]
    here = os.path.dirname(os.path.realpath(__file__))
    package_dir = os.path.dirname(here)
    bad = [p for p in sys.path if os.path.realpath(p or os.getcwd()) in (package_dir, here)]
    if bad:
        sys.exit(f"vtech_baby worker: {bad[0]} is on sys.path; its modules (select.py, ...) would shadow "
                 "others of the same name. Start the worker with python -I.")
    _register_parents(package, package_dir)
    worker = importlib.import_module(f"{package}.vtech_lan.worker")
    sys.exit(worker.main(sys.argv[2:]))


if __name__ == "__main__":
    _start()
