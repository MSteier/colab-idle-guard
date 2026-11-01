# Colab Idle Guard for VS Code

Reports interaction with **Colab-backed** Jupyter notebooks, and their kernel
execution state, to the Colab Idle Guard app running on this computer
(`127.0.0.1`). The app uses this to release idle Colab runtimes. Local
Jupyter kernels are never monitored.

- **Colab detection:** once per kernel session, while the kernel is idle, a
  read-only one-line probe checks for Colab's runtime marker. The Jupyter
  extension asks you once to allow kernel access. Set
  `colabIdleGuard.detection` to `manual` to disable the probe, then use
  *Colab Idle Guard: Treat this notebook as a Colab notebook*.
- **Execution state:** the Jupyter kernel's own busy/idle status.
- **Pairing:** automatic. The extension reads the app's DPAPI-protected token
  file for your Windows account. Use *Colab Idle Guard: Pair with Colab Idle
  Guard app* to paste a token manually instead.
- **Privacy:** only event kinds (edit, key, click, scroll, focus), counts and
  opaque notebook IDs are sent. No code, outputs, file names or typed text.

See the project's docs folder for the full manual.
