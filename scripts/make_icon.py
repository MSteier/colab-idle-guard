"""Writes the application .ico (multi-size) from the same artwork as the tray icon."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from colab_idle_guard.desktop.icons import state_icon  # noqa: E402

out = Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
state_icon("active").save(out, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
print(out)
