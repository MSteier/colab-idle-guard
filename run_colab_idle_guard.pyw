"""Windowless launcher (pythonw) used by the Start-with-Windows entry and shortcuts."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from colab_idle_guard.__main__ import main  # noqa: E402

sys.exit(main())
