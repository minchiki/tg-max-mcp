"""Контракт инструментов: у каждого есть понятное описание (его читает модель) и ни у каких двух нет одного имени."""
import inspect
import os
import sys
import tempfile

os.environ.setdefault("UMS_FILE", os.path.join(tempfile.mkdtemp(), "ums.json"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import mes  # noqa: E402
import umschool  # noqa: E402


def collect(module):
    found = {}

    def tool(write=False, **_):
        def deco(fn):
            assert fn.__name__ not in found, f"дубль {fn.__name__}"
            found[fn.__name__] = fn
            return fn
        return deco

    module.register(tool)
    return found


def test_every_tool_is_documented_and_prefixed():
    for mod, prefix in ((mes, "mes_"), (umschool, "umschool_")):
        tools = collect(mod)
        assert tools
        for name, fn in tools.items():
            assert name.startswith(prefix), name
            assert inspect.iscoroutinefunction(fn), name
            doc = inspect.getdoc(fn) or ""
            assert len(doc) >= 25, f"{name}: нет нормального описания"
            for pname, prm in inspect.signature(fn).parameters.items():
                assert prm.annotation is not inspect.Parameter.empty, f"{name}.{pname}: нет типа"
