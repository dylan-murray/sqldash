"""Guarantee `tests/` is importable however pytest is invoked.

`from helpers import ...` relies on pytest's default prepend import mode
putting this directory on sys.path — which stops being true the moment anyone
adds tests/__init__.py or switches to --import-mode=importlib. conftest.py is
always loaded first, so pinning the path here keeps the shared helpers working
under any import mode.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
