// Reports user interaction with Colab-backed notebooks only.
//
// Counts selection changes, scrolling, cell edits and typing in a monitored
// notebook while the VS Code window has focus. Programmatic changes such as
// streaming outputs or execution summaries are deliberately ignored: they are
// not interaction. Only the event kind is reported, never text or content.

import * as vscode from "vscode";
import type { ActivityKind } from "./protocol";
import type { ExecutionMonitor } from "./execution_monitor";

const THROTTLE_MS = 5000;

export class ActivityMonitor implements vscode.Disposable {
  private readonly disposables: vscode.Disposable[] = [];
  private readonly lastSent = new Map<ActivityKind, number>();

  constructor(
    private readonly execution: ExecutionMonitor,
    private readonly emit: (kind: ActivityKind) => void,
  ) {
    const w = vscode.window;
    const ws = vscode.workspace;
    this.disposables.push(
      w.onDidChangeNotebookEditorSelection((e) => this.fromNotebook(e.notebookEditor.notebook, "click")),
      w.onDidChangeNotebookEditorVisibleRanges((e) => this.fromNotebook(e.notebookEditor.notebook, "scroll")),
      w.onDidChangeActiveNotebookEditor((editor) => editor && this.fromNotebook(editor.notebook, "focus")),
      w.onDidChangeWindowState((s) => {
        const nb = w.activeNotebookEditor?.notebook;
        if (s.focused && nb) this.fromNotebook(nb, "focus");
      }),
      ws.onDidChangeNotebookDocument((e) => {
        // Only structural edits and cell source edits; outputs/executionSummary are not interaction.
        const edited = e.contentChanges.length > 0 || e.cellChanges.some((c) => c.document !== undefined);
        if (edited) this.fromNotebook(e.notebook, "edit");
      }),
      ws.onDidChangeTextDocument((e) => {
        if (e.document.uri.scheme !== "vscode-notebook-cell" || e.contentChanges.length === 0) return;
        const nb = this.notebookOfCell(e.document);
        if (nb) this.fromNotebook(nb, "key");
      }),
      w.onDidChangeTextEditorSelection((e) => {
        if (e.textEditor.document.uri.scheme !== "vscode-notebook-cell") return;
        if (e.kind === vscode.TextEditorSelectionChangeKind.Command) return; // programmatic
        const nb = this.notebookOfCell(e.textEditor.document);
        if (nb) this.fromNotebook(nb, e.kind === vscode.TextEditorSelectionChangeKind.Mouse ? "click" : "key");
      }),
    );
  }

  dispose(): void {
    for (const d of this.disposables) d.dispose();
  }

  private notebookOfCell(cellDoc: vscode.TextDocument): vscode.NotebookDocument | undefined {
    for (const nb of vscode.workspace.notebookDocuments) {
      if (nb.getCells().some((c) => c.document === cellDoc)) return nb;
    }
    return undefined;
  }

  private fromNotebook(nb: vscode.NotebookDocument, kind: ActivityKind): void {
    if (!vscode.window.state.focused) return; // VS Code is not the foreground app
    const tracker = this.execution.trackerFor(nb);
    if (!tracker || !tracker.monitored) return; // not a Colab notebook
    const now = Date.now();
    if (now - (this.lastSent.get(kind) ?? 0) < THROTTLE_MS) return;
    this.lastSent.set(kind, now);
    this.emit(kind);
  }
}
