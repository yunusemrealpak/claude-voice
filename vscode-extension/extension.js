// Claude Voice Guide: open a file and highlight a line range on request.
//
// Two layers: /show marks the block being explained (soft), and /focus puts a
// stronger mark on the lines the narration is talking about right now, moving
// as the words are spoken.
//
// claude-voice drives it through a URI rather than a port, so it works with any
// number of VS Code windows and needs no server: `open vscode://...` reaches the
// window that was used last, which the walkthrough brings forward with `code <root>`.

const vscode = require("vscode");

let highlight;
let emphasis;
let highlighted = [];
let shown = null; // the document of the last /show, which /focus applies to

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
  emphasis = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: "rgba(255, 150, 0, 0.30)",
    borderWidth: "0 0 0 6px",
    borderStyle: "solid",
    borderColor: "rgba(255, 110, 0, 1)",
    overviewRulerColor: "rgba(255, 110, 0, 1)",
    overviewRulerLane: vscode.OverviewRulerLane.Full,
  });
  context.subscriptions.push(
    highlight,
    emphasis,
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
  if (uri.path === "/focus") {
    focus(new URLSearchParams(uri.query));
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
  shown = document;
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

// Emphasis on lines of the shown file, or none without a start. Only while that
// file is on screen: the narration must not pull the user away from elsewhere.
function focus(params) {
  const editor = shown && vscode.window.visibleTextEditors.find((e) => e.document === shown);
  if (!editor) {
    return;
  }
  if (!params.get("start")) {
    editor.setDecorations(emphasis, []);
    return;
  }
  const first = clamp(parseInt(params.get("start"), 10), 1, shown.lineCount) - 1;
  const last = clamp(parseInt(params.get("end") || String(first + 1), 10), first + 1, shown.lineCount) - 1;
  const range = new vscode.Range(first, 0, last, shown.lineAt(last).text.length);
  editor.setDecorations(emphasis, [range]);
  editor.revealRange(range, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
}

function clear() {
  for (const editor of highlighted) {
    editor.setDecorations(highlight, []);
    editor.setDecorations(emphasis, []);
  }
  highlighted = [];
  shown = null;
}

function clamp(value, low, high) {
  if (Number.isNaN(value)) {
    return low;
  }
  return Math.min(Math.max(value, low), high);
}

function deactivate() {}

module.exports = { activate, deactivate };
