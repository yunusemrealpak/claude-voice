# claude-voice

Spoken conversation with Claude Code. `/talk` in any session starts listening to
the microphone. Claude answers aloud through ElevenLabs, and the terminal keeps
working as usual. Start talking while Claude speaks and it stops mid-word; what
you say becomes the next message.

```
microphone ─► voice daemon ─► Deepgram (streaming, Turkish) ─► whole spoken turn
                  │                                                   │
headphones ◄──────┤◄── ElevenLabs (streaming) ◄── voicectl speak      ▼
                  └── Unix socket ◄── voicectl listen ◄── Monitor ─► Claude
```

- **The daemon** (`voice/daemon.py`) owns the microphone, the headphones and both
  cloud streams. Barge-in lives here and never waits for Claude. Words heard
  while speech is playing clear the playback buffer within one 20 ms block and
  drop anything queued behind it.
- **Input to Claude.** `voicectl listen` prints one line per spoken turn and runs
  under Claude Code's Monitor tool, so each turn arrives in the session as an
  event, even while Claude is in the middle of other work. A turn is held until
  a listener is attached, so re-arming the 30-minute Monitor loses nothing.
- **Output from Claude.** `voicectl speak "..."` queues speech and returns.
  `--wait` blocks until the speech ends. If the user interrupts, it returns what
  they said instead.
- **The skill** (`skill/talk/SKILL.md`) tells Claude how to behave in voice
  mode. It answers aloud first and then works. It speaks at milestones only, not
  at every step. Destructive or outward-facing actions need a *typed*
  confirmation, because the microphone also hears the room.

## Setup

Needs macOS, Python 3.12 (`brew install python@3.12`), a Deepgram API key, an
ElevenLabs API key and an ElevenLabs voice to speak with.

```bash
bin/install          # virtualenv, dependencies, .env from .env.example,
                     # ~/.claude/skills/talk symlink, VS Code extension
$EDITOR .env         # DEEPGRAM_API_KEY, ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID
bin/voicectl selftest
```

Credentials and the voice id are read only from the environment or the
git-ignored `.env`, never from a tracked file. Machine-specific settings, such as
your microphone's name, go in `config.local.toml`, which is also git-ignored and
overrides `config.toml` section by section:

```toml
[audio]
mic = "MacBook Pro Microphone"   # a substring of a name from `voicectl devices`
```

`selftest` makes a round trip through the real services without touching any
audio device. ElevenLabs speaks a sentence, and the audio streams to Deepgram at
real-time pace. It prints what came back and the latencies.

The first `voicectl start` from a terminal triggers macOS's microphone prompt
for that terminal app. Until it is answered, the daemon waits inside
CoreAudio.

**Use headphones.** With speakers, the microphone hears Claude's own voice and
the daemon takes it for the user interrupting.

## Commands

| | |
|---|---|
| `voicectl start` / `shutdown` / `status` | daemon lifecycle |
| `voicectl speak [--wait] TEXT` | say something (`-` reads stdin) |
| `voicectl hush` | stop speaking now |
| `voicectl mute` / `unmute` | stop / resume sending the microphone to Deepgram |
| `voicectl listen` | one line per spoken turn, for Monitor |
| `voicectl inject TEXT` | act as if the user had said TEXT (testing) |
| `voicectl show FILE START END` | open FILE in VS Code with those lines highlighted (`--clear` removes it) |
| `voicectl devices` | audio devices as PortAudio sees them |
| `voicectl selftest [TEXT]` | TTS → STT round trip |

State lives in `~/.claude-voice/`:

- `voice.sock` is the socket.
- `daemon.log` is the log.
- `transcript.jsonl` records both sides of the conversation with timestamps.

## Code walkthroughs

Ask for a walkthrough in voice mode ("walk me through the architecture"). Claude
plans the stops, then goes through them one at a time:

1. It highlights each block in VS Code with `voicectl show`.
2. It explains the block with `voicectl speak --wait`.
3. If you interrupt with a question, it answers and then asks whether to go on.

`vscode-extension/` is the small extension behind `show`. It registers a
`vscode://claude-voice.claude-voice-guide/show?path=…&start=…&end=…` URI
handler, which opens the file, highlights the lines and centres them.
`bin/install` packages and installs it.

The design uses a URI rather than screen automation. Every step is one
deterministic call, with no screenshots and no clicks. The first time, VS Code
may ask whether the extension may open URIs; allow it and tick "don't ask
again".

## Configuration

`config.toml` holds the settings. Every value is optional, and the defaults are
in `voice/config.py`.

- **`[stt] language` and `[tts] language`** are the language you speak and the
  language Claude answers in. Both default to Turkish (`tr`). Set both to your
  own, for example `en`, and Claude follows in the language it speaks.

- **`[audio] mic`** defaults to the system input. With Bluetooth headphones, set
  it to the computer's built-in microphone in `config.local.toml`. Opening a
  headset's microphone switches macOS to the headset profile, and playback drops
  to phone quality.
- **`[stt] turn_grace_ms` and `turn_grace_incomplete_ms`** set how long to wait
  after Deepgram reports silence. Deepgram's punctuation picks which one
  applies. A finished sentence gets 700 ms. Words that stop mid-sentence ("Ya
  ben mikrofonu") are a breath, not the end, and get 2.5 s. Raise the second one
  if long thinking pauses still split your messages.
- **`[stt] keyterms`** are words Deepgram would otherwise mishear in Turkish
  speech. Without them, "Claude" came back as "Cloud".
- **`[audio] lead_in_ms` and `keep_awake`** exist because of Bluetooth earbuds.
  After silence they swallow the first word of a reply. ElevenLabs output is
  not the cause: measured, its first 350 ms is 1–7 dB *louder* than the rest.
  So every reply starts with a 300 ms pause. Between replies the daemon plays
  near-inaudible noise (`keep_awake_dbfs`, −70 dBFS) instead of digital
  silence, so the link never falls asleep. If first words still go missing,
  raise the level towards −60.
- **`[daemon] reply_wait_s`** is how long the user must stay quiet before an
  interrupted `speak --wait` stops waiting for their words. It is not a total
  and restarts with every word, so a 30-second interruption is still returned
  as the answer.
- **`[daemon] idle_shutdown_s`** is how long the daemon runs with no listener
  attached before it exits. This keeps a closed session from leaving the
  microphone streaming.

## Measured (Apple Silicon laptop, Bluetooth earbuds, flash v2.5)

| | |
|---|---|
| ElevenLabs first audio, warm socket | ~250 ms |
| ElevenLabs first audio, cold socket | up to 2.5 s, so a spare socket is kept open and refreshed |
| end of speech → turn delivered | ~1.2 s (300 ms endpointing + 700 ms grace + network) |
| turn → Claude sees it mid-task | at the next tool-call boundary |
| barge-in | within one Deepgram interim result, typically 300–500 ms |
| end of speech → turn, sentence left unfinished | ~2.5 s, a breath rather than the end |

The rest of the response time is Claude thinking. Lower reasoning effort makes
voice mode noticeably snappier.

## Why audio is never resampled in Python

Both streams are opened at the rate the cloud side uses: 16 kHz up and 24 kHz
down. CoreAudio converts to the device rate in native code. Bluetooth headphones
run at 44.1 kHz, and 24000 → 44100 is a 147/80 ratio. The numpy polyphase filter
for that ratio cost 147 ms of CPU per 100 ms of audio. It froze the event loop,
and with it barge-in, for as long as Claude spoke.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The tests cover turn assembly from Deepgram messages and the daemon's socket
protocol, barge-in and delivery rules. They use fake audio devices and
providers.
