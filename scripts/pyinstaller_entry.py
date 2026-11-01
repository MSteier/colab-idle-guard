"""PyInstaller entry point for the Colab Idle Guard executable."""

import sys

from colab_idle_guard.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
