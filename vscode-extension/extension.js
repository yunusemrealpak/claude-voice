// Claude Voice Guide: open a file and highlight a line range on request.
//
// claude-voice drives it through a URI rather than a port, so it works with any
// number of VS Code windows and needs no server: `open vscode://...` reaches the
// window that was used last, which the walkthrough brings forward with `code <root>`.

const vscode = require("vscode");

let highlight;
let highlighted = [];

function activate(context) {
  highlight = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: "rgba(255, 196, 0, 0.16)",
    borderWidth: "0 0 0 3px",
    borderStyle: "solid",
    borderColor: "rgba(255, 170, 0, 0.9)",
    overviewRulerColor: "rgba(255, 170, 0, 0.9)",
    overviewRulerLane: vscode.OverviewRulerLane.Full,
  });
  context.subscriptions.push(
    highlight,
    vscode.window.registerUriHandler({
      handleUri: (uri) =>
        handle(uri).catch((error) =>
          vscode.window.showErrorMessage(`Claude Voice Guide: ${error.message}`)
        ),
    }),
    vscode.commands.registerCommand("claudeVoiceGuide.clear", clear)
  );
}

async function handle(uri) {
  if (uri.path === "/clear") {
    clear();
    return;
  }
  if (uri.path !== "/show") {
    throw new Error(`unknown action ${uri.path}`);
  }
  const params = new URLSearchParams(uri.query);
  const file = params.get("path");
  if (!file) {
    throw new Error("no path given");
  }
  const document = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
  const editor = await vscode.window.showTextDocument(document, { preview: false });

  const first = clamp(parseInt(params.get("start") || "1", 10), 1, document.lineCount) - 1;
  const last = clamp(parseInt(params.get("end") || String(first + 1), 10), first + 1, document.lineCount) - 1;
  const range = new vscode.Range(first, 0, last, document.lineAt(last).text.length);

  clear();
  editor.setDecorations(highlight, [range]);
  highlighted = [editor];
  editor.selection = new vscode.Selection(range.start, range.start);

  // A block that fits on screen goes to the middle; a longer one starts at the
  // top so its beginning, which is what gets explained first, stays visible.
  const visible = editor.visibleRanges[0];
  const screen = visible ? visible.end.line - visible.start.line : 30;
  const type = last - first < screen - 4
    ? vscode.TextEditorRevealType.InCenter
    : vscode.TextEditorRevealType.AtTop;
  editor.revealRange(range, type);
}

function clear() {
  for (const editor of highlighted) {
    editor.setDecorations(highlight, []);
  }
  highlighted = [];
}

function clamp(value, low, high) {
  if (Number.isNaN(value)) {
    return low;
  }
  return Math.min(Math.max(value, low), high);
}

function deactivate() {}

module.exports = { activate, deactivate };
