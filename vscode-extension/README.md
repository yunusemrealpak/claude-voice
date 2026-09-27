# Claude Voice Guide

The VS Code half of claude-voice's code walkthroughs. `voicectl show FILE START END`
opens a `vscode://claude-voice.claude-voice-guide/show?...` URI; this extension
opens the file, highlights the lines and brings them to the middle of the screen
while Claude explains them aloud.

- `/show?path=<absolute path>&start=<line>&end=<line>` — open, highlight, reveal
- `/focus?start=<line>&end=<line>` — a stronger highlight on those lines of the
  file last shown, for the lines the narration is on right now; `/focus` alone
  removes it. Ignored unless that file is on screen.
- `/clear` — remove both highlights
