"""
Shared test configuration.

Sets fake AWS credentials so boto3/moto never attempt real calls,
and provides load_handler() to import each Lambda handler under a
unique module name (all three files are called handler.py).
"""
import importlib.util
import os
import sys

# Fake credentials — moto requires these to be set
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Make shared utilities importable
sys.path.insert(0, os.path.join(_BACKEND, "shared"))


def load_handler(function_name: str):
    """
    Load backend/functions/{function_name}/handler.py as a uniquely-named
    module so multiple handler.py files can coexist in sys.modules.
    Returns the cached module on repeated calls.
    """
    module_name = f"{function_name}_handler"
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = os.path.join(_BACKEND, "functions", function_name, "handler.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod
