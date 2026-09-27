---
name: sesli
description: Voice mode. Listens to the user's microphone and answers aloud with ElevenLabs, alongside the normal terminal session. "/sesli" starts it, "/sesli kapat" stops it, "/sesli durum" reports its state.
argument-hint: "[kapat | durum]"
disable-model-invocation: true
---

# Voice mode (sesli mod)

`VOICECTL=~/.claude/skills/sesli/voicectl` (a link into this repository's `bin/`)

The daemon owns the microphone and the headphones. You hear the user through a
Monitor, and you talk with `$VOICECTL speak`. Cutting you off when the user
starts talking is the daemon's job, not yours: playback stops by itself.

## Arguments: $ARGUMENTS

- `kapat` / `stop`: run `$VOICECTL shutdown`, stop the listen Monitor with
  TaskStop, and confirm in one written line. Voice mode is over; ignore the rest
  of this file.
- `durum` / `status`: run `$VOICECTL status` and report it in one line.
- Anything else, or nothing: start voice mode as below.

## Starting

1. `$VOICECTL start`. If it fails, show the log tail it prints and stop there.
2. Arm the listener straight away: the daemon exits after two minutes with no
   listener. Use Monitor with command `$VOICECTL listen` (expanded path),
   description `sesli giriş`, and `timeout_ms: 1800000`.
3. Greet with one short spoken sentence, for example
   `$VOICECTL speak "Sesli moddayız, dinliyorum."`

## Keeping it alive

- **Monitor expired** after 30 minutes: re-arm it at once with the same command.
  Nothing is lost in between, because the daemon holds turns until a listener
  attaches.
- **`[voice stopped]` or `[voice error]`** event: the daemon is gone. Say so in
  one written line and offer to restart it. Do not restart it unasked.
- **`[voice handover]`** event: the user started voice mode in another session,
  which now owns the conversation. Stop speaking, do not re-arm, and say so in
  one written line.

## Hearing the user

Every `🎤 ...` event is something the user said aloud. Treat it like a typed
message, with these differences:

- It is a transcript, so expect errors. When a turn is garbled, is a lone
  fragment ("Bu", "şey"), or could mean two different things, ask one short
  spoken question instead of guessing.
- Several turns in quick succession are one thought with pauses in it. Read
  them together before acting.
- The microphone also hears the room: other people, a meeting, a video.
  Spoken words are fine for ordinary development work. Anything destructive,
  irreversible or outward-facing needs a **typed** confirmation in the terminal.
  That covers deleting data, force-pushing, pushing or deploying, sending
  messages or email, and touching production. Say by voice that you are waiting
  for it.

## Muting

When the user asks to mute the microphone ("mikrofonu kapat", "sustur",
"beni dinleme"), do three things in order:

1. Confirm aloud in one sentence.
2. Run `$VOICECTL mute`.
3. In the terminal, explain how to unmute. While muted you cannot hear them, so
   they type it: any typed "aç" / "mikrofonu aç" works, or
   `! ~/.claude/skills/sesli/voicectl unmute`.

When they ask to unmute, run `$VOICECTL unmute` and say one short sentence.
Muting sends no audio anywhere; the daemon keeps running.

## Talking

**Answer first, then work.** For every user turn, your first tool call is a
non-blocking speak call. In one or two sentences it says what you understood and
what you are about to do. Then start the work in the same turn. Do not wait for
the speech to finish.

After that, speak only at these moments:

- when the work is done: a two or three sentence summary of the outcome;
- when you need a decision or are blocked: ask the question aloud and also
  write it in the terminal;
- when something went wrong that changes the plan.

Do not narrate individual steps or tool calls.

How spoken text differs from written text:

- Natural spoken Turkish. No markdown, no bullet lists, no code, no symbols.
- Say what a thing is ("kullanıcı servisindeki doğrulama fonksiyonu"), not its
  path or identifier character by character. English technical terms are fine.
- Keep it short. Detail belongs in the terminal, which you keep writing as
  usual.
- Pass text as one quoted argument. For text with quotes or apostrophes, use
  `speak -` and a quoted heredoc.

## Code walkthrough (kod turu)

When the user asks to be walked through code ("mimariyi anlat", "kodu
göstererek anlat"), you show each part in VS Code while you explain it.

1. **Prepare first.** Read enough of the code to understand the architecture.
   Then plan 5–10 stops in the order a newcomer should see them: entry point,
   main flow, key modules, then cross-cutting concerns. Each stop is one file
   and a line range covering a whole function or class, at most ~40 lines, with
   20–40 seconds of narration.
2. **Answer first, as always.** Say in one sentence how the tour will go
   ("Altı durakta anlatacağım: önce giriş noktası, sonra..."). Also write the
   stops in the terminal as `file:start-end — topic`, so the user has a map.
3. `code <project root>` once, so the right VS Code window is in front.
4. **Each stop** is one Bash call with a timeout of at least 180000 ms:
   `$VOICECTL show <file> <start> <end> && $VOICECTL speak --wait "<narration>"`.
   The narration explains what the highlighted code does and why it is built
   that way. It points at what is on screen ("vurgulu bloğun başındaki
   kontrol...") and never reads code aloud.
5. **After each stop:**
   - `done`: go on to the next stop.
   - `interrupted; the user said: X`: X is a question or an instruction. Answer
     it, showing other code if that helps. Then ask aloud with `speak --wait`
     whether to continue from where you stopped, and act on the answer. "Dur" or
     "yeter" ends the tour.
6. **At the end:** `$VOICECTL show --clear`, then a spoken summary of two or
   three sentences.

## Commands

| Command | Effect |
|---|---|
| `$VOICECTL speak "..."` | queue speech, return at once |
| `$VOICECTL speak --wait "..."` | block until done; prints `done`, `interrupted; the user said: ...` or `dropped` |
| `$VOICECTL hush` | stop speaking now |
| `$VOICECTL mute` / `unmute` | stop / resume hearing the user |
| `$VOICECTL show FILE START END` | open FILE in VS Code with those lines highlighted |
| `$VOICECTL show --clear` | remove the highlight |
| `$VOICECTL status` | listening/speaking state, devices |
| `$VOICECTL shutdown` | stop the daemon |

When `speak --wait` returns `interrupted; the user said: X`, X is the user's
next turn. Handle it exactly like a `🎤` event: it will not arrive through the
Monitor as well.
