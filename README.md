# browseruse

Drive your own Brave or Chrome by typing at it.

It attaches to **your** browser over the Chrome DevTools Protocol — your logins,
your cookies, your extensions — reads the page as a numbered list of things it
can click, and works through whatever you asked for. Claude does the thinking.
[Jev](https://docs.typesafe.ai) makes the fast typed calls inside the loop, and
gates every action that would change something.

```
you> find the cheapest direct flight from Vienna to Lisbon in late October

  -> Opened https://www.google.com/travel/flights
  -> Typed into [14] and pressed Enter.
  i  index 27 no longer exists; Jev re-matched 'the Direct flights toggle' to [31] at 93% confidence
  -> Clicked [31] "Direct flights". Now at https://www.google.com/travel/flights?...

┌─ Approve this? ────────────────────────────────────┐
│ click  [44] button "Select flight — €189"          │
│                                                     │
│ risk 2.1/3 (confidence 88%) — Commits something     │
│ outward-facing or costly                            │
└─────────────────────────────────────────────────────┘
  Go ahead? [y/n] (n):
```

## Why two models

Jev is a **System One** model: you hand it a state and typed questions, it hands
back answers with calibrated probabilities in well under a second. It does not
generate text, so it cannot turn "book me a flight" into a plan — that is
Claude's job. What it is unreasonably good at is the question this loop asks
over and over on every single step:

| Question | Jev primitive | What it buys |
|---|---|---|
| How consequential is this action? | `Score` over a 4-level rubric | The confirmation gate. Nothing spends money or sends mail without you. |
| What kind of page is this? | `Choice` — normal / cookie wall / login / captcha / error / loading | The loop handles the boring cases itself and stops on the ones that are yours. |
| Is the goal already met? | `Noul` (yes/no probability) | Stops without burning another Claude turn. |
| Which element did Claude mean? | `Choice` over up to 255 labels | Recovers from index drift when a page re-renders mid-step — the single most common way browser agents click the wrong thing. |

The first three ride in **one** request per step, because `system_one` answers a
whole mapping of named questions at once. Jev's input is billed at
$0.042/MTok and its output at $0, so the entire gating layer costs a rounding
error next to one Claude turn.

That 255 in the last row is not a coincidence: `Choice` takes at most 255
labels, so the DOM indexer caps its element list at 255 and the element list
*is* the choice set.

## Setup

Python 3.11+, and Brave or Chrome installed.

```bash
git clone https://github.com/Roxeraf/BroswerUse
cd BroswerUse
python -m venv .venv

# macOS
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -e .
playwright install-deps       # Linux only; skip on macOS and Windows

cp .env.example .env          # then put your two keys in it
```

You need `ANTHROPIC_API_KEY` and `TYPESAFE_API_KEY`. Without the Jev key it
still runs, but with no risk scoring and no page classification — so it asks
before every action that changes anything.

## Running it

```bash
browseruse                              # chat, driving Brave
browseruse --browser chrome             # chat, driving Chrome
browseruse "open my github notifications and summarise them"   # one task, then exit
browseruse --read-only                  # navigate freely, confirm every click
```

Commands inside the chat: `/tabs`, `/goto <url>`, `/readonly`, `/risk <0-3>`,
`/browser`, `/help`, `/quit`.

### Which profile it uses

By default it launches your browser with a **separate automation profile** under
`~/.browseruse/profiles/`. Clean, no conflicts — but no logins, so you sign in
once inside that window and it remembers from then on.

To use your everyday profile instead, with everything you are already signed
into:

```bash
browseruse --real-profile
```

**Quit Brave/Chrome completely first.** Chromium hands the command line to an
already-running instance and exits, and that instance has no debugging port —
so there would be nothing to attach to. You get a clear error if this happens
rather than a hang.

On macOS, ⌘Q, not just closing the window. On Windows, check the tray.

## Safety

Two independent checks, in this order, before anything that changes state:

1. **A hard-coded list** that never runs unattended regardless of any score —
   payments, transfers, account deletion, cancellations. Credentials, 2FA and
   one-time codes are refused outright: the agent will not type them, and asks
   you to do that part yourself.
2. **Jev's risk score** against your threshold (default 1.5 of 3).

If Jev is unreachable the gate fails **closed** — a missing score is not a low
one. And a declined action is final: the system prompt tells Claude not to retry
it or route around it.

## Voice

Text only for now. `browseruse/voice/` defines the two interfaces a speech
backend has to satisfy (`SpeechToText.listen()`, `TextToSpeech.speak()`), so
wiring in local Whisper later is one class, not a rewrite of the loop.

## Layout

```
src/browseruse/
├── cli.py              chat REPL, approval prompts
├── config.py           settings from the environment
├── safety.py           blocklist + risk threshold -> allow / confirm / block
├── browser/
│   ├── launcher.py     find Brave/Chrome on macOS, Windows, Linux; open a CDP port
│   ├── session.py      the CDP connection, tabs, navigation
│   ├── dom.py          page -> numbered list of interactive elements (max 255)
│   └── actions.py      click, type, scroll, select, tabs, extract
├── agent/
│   ├── loop.py         Claude decides -> Jev gates -> the browser acts
│   ├── jev.py          the four typed decisions
│   ├── tools.py        tool schemas (frozen order, for the prompt cache)
│   └── prompts.py      the system prompt
└── voice/              interfaces only, for now
```

## What leaves your machine

Worth being precise about, since this thing reads your logged-in pages.

**Two destinations, both of them API calls you are paying for.** There is no
telemetry, no analytics, no crash reporting, no phone-home. The only hardcoded
network address in the source is `http://127.0.0.1`, the debugging port on your
own machine. Nothing is written to disk except the browser profile itself —
the conversation lives in memory and dies with the process.

**On every step, Claude receives:** the page URL and title, the numbered
element list (labels, roles, link targets), up to 4000 characters of the page's
visible text, and your instruction.

**On every step, Jev receives:** the URL and title, up to 2000 characters of
visible text, and element labels. For a risk score it also gets the action and
its arguments.

So: whatever is on the pages you point it at goes to Anthropic and TypeSafe,
the same way it would with any browser agent. If a page is confidential, do not
point it at that page. What each provider does with it afterwards is governed
by their terms, not by this code.

### What is held back

Password, card-number, CVV and one-time-code fields are detected three ways —
the input `type`, the `autocomplete` hint your browser uses to autofill, and
the field's name, id, placeholder or label — and **their contents are never
captured**. The agent is told `(sensitive field, already filled)` so it can
still reason about the form; it cannot see what is in it.

The refusal to type into such a field happens *before* the risk call, not
after. That ordering is the whole point: scoring the action would have
transmitted the secret. Same for one-time codes mentioned in the text itself.

`tests/test_no_secret_leaks.py` pins this shut: it loads a login page with a
filled-in password, card number and CVV, and asserts none of the three appear
in anything bound for either model.

**What is not redacted:** ordinary form values, including email addresses and
usernames, because the agent genuinely needs them ("which account am I signed
into?"). Visible page text is sent as-is.

### Your browser and your logins

With `--real-profile` the agent is inside your logged-in session and can act as
you on any site you are signed into. That is the feature. The safety gate is
what stands between that and something you did not want: a hard blocklist for
payments, transfers and deletions that ignores the risk score entirely, then
the score itself. Extensions, cookies and history behave exactly as normal —
nothing here modifies them.

Without `--real-profile` it uses a separate profile under `~/.browseruse/`,
which starts with no logins at all.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

83 tests. The DOM and action tests drive a real headless Chromium against
`tests/fixtures/shop.html` and are skipped if no Chromium is installed; the Jev
tests run against a mocked API and assert the wire shapes (including that the
element `Choice` never exceeds 255 labels); the loop tests run the full
orchestration against a scripted Claude and a fake browser, covering the
approval gate, declined actions, Jev outages and stale-index recovery; and
`test_no_secret_leaks.py` asserts that no credential reaches either model.

## Known limits

- Claude is told to act one step at a time. If it ever batches two actions, the
  second is evaluated against the snapshot taken before the first ran — the
  index re-match catches most of that, but not all of it.


- Cross-origin iframes are not addressable. Same-origin ones are.
- Captchas are not solved, by design. It stops and hands the window back.
- The element list is viewport-biased: off-screen elements are listed but marked
  `(below the fold)` and need a scroll first.
