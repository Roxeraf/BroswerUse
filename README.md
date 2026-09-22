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

Commands inside the chat: `/tabs`, `/goto <url>`, `/readonly`, `/risk <0-3>`, `/cost`,
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

## What it costs

Every task prints what it cost, measured from the `usage` both APIs report —
not estimated. `/cost` shows the session total:

```
Model: claude-opus-5

  Claude     12 turns     190,800 in /   5,400 out   $0.3923
           of the input: 162,635 cached (85%), 28,165 written, 0 fresh
  Jev        24 calls      25,416 in /       0 out   $0.0011

  TOTAL                                                     $0.3934

  Jev is 368x cheaper here.
```

Rough guide, from measured prompt sizes at Opus 5 rates ($5/$25 per MTok,
cache reads 0.1x, writes 1.25x):

| Task | Steps | Claude | Jev | Total |
|---|---|---|---|---|
| check a page, read something | 3 | $0.08 | $0.0003 | **~$0.08** |
| search and summarise results | 6 | $0.17 | $0.0005 | **~$0.17** |
| book a flight, up to the payment form | 12 | $0.39 | $0.0011 | **~$0.39** |
| long multi-site research | 25 | $0.99 | $0.0022 | **~$0.99** |
| hits the 40-step budget | 40 | $1.91 | $0.0036 | **~$1.92** |

Jev is **0.3% of the bill**. The gating layer — risk scores, page
classification, done-checks, element re-matching, two calls per step — costs
about a tenth of a cent on a twelve-step task. Claude is essentially the
entire cost.

Cost grows **faster than linearly** with steps, because the whole conversation
is resent each turn. Prompt caching takes most of the sting out (85% of input
tokens are cache reads at 0.1x by step 12, and without it that flight booking
would be $1.09 instead of $0.39) but the curve still bends upward. That is what
`BROWSERUSE_MAX_STEPS` is protecting you from.

### Where the money actually goes

Not where you would guess. On that twelve-step booking:

| | tokens | cost | share |
|---|---|---|---|
| cache **writes** — the new page, each step (1.25x) | 28,165 | $0.176 | **45%** |
| **output** — thinking + the tool call (25x) | 5,400 | $0.135 | **34%** |
| cache **reads** — the whole history (0.1x) | 162,635 | $0.081 | 21% |

Resending the conversation is the *cheapest* part; caching already solved that.
Trimming old history would save reads at $0.50/MTok and force rewrites at
$6.25/MTok — a 12x loss. The two things that matter are **how much you send per
step** and **how many steps need Claude at all**. Jev can help with both.

### Jev's fast path

Most browser steps are not reasoning. "The cookie banner is up, dismiss it."
"The results are below, scroll." "That is obviously the Search button." Paying
Opus to think about those is the waste.

So Jev is asked, in the same `system_one` call it already makes, two more
questions: *what is the obvious next move?* (`Choice` over click / scroll /
go_back / ask_claude) and *which element?* (`Choice` over the live list). When
it is confident on both, the step runs without a Claude turn at all.

The proposal is **free** — it rides along in a request that was already
happening, adding 1,291 tokens, $0.00065 across the whole task.

Hard limits, because a confident wrong model is worse than a slow one:

- **Only pure selections.** Jev cannot generate text, so typing a search query
  is always Claude's. `AUTOPILOT_ACTIONS` has no `type_text` and a test asserts
  it never will.
- **80% confidence on both** the action and the element.
- **Three consecutive steps** maximum, then Claude gets a turn regardless.
- **Ordinary pages only** — a cookie wall, login or captcha hands back.
- **The safety gate is not skipped.** Autopilot bypasses Claude, not the risk
  score. Anything that would need your approval stops the run and hands back to
  Claude rather than asking you out of context.

Claude is told what happened in its absence (`Steps already taken for you: ...`),
so the history stays honest.

### Withholding the page text

A third new question: *does this goal need the page's prose, or just its
controls?* On a navigation step the 4,000-character text block is dead weight —
one observation drops from 1,780 tokens to 1,222, **31% smaller**. Claude is
told it was withheld and can call `extract_text` to fetch it. The text is kept
on anything but a confident no, because a wrong drop costs quality.

### What it adds up to

Same twelve-step booking, same measured prompt sizes:

| | Claude turns | cost | saved |
|---|---|---|---|
| Claude every step, full page text | 12 | $0.393 | — |
| + withhold prose when navigational | 12 | $0.363 | 8% |
| + Jev autopilot on 25% of steps | 9 | $0.263 | **33%** |
| + Jev autopilot on 40% of steps | 7 | $0.200 | **49%** |
| + Jev autopilot on 55% of steps | 5 | $0.142 | **64%** |
| ...and on Sonnet 5 | 7 | $0.080 | **80%** |

The saving beats the share of steps removed, because cost grows faster than
linearly in turns — every turn you delete also stops resending history.

Which rate you actually get depends on the site, so `/cost` tells you: compare
`Claude turns` against the step count. Turn it off with
`BROWSERUSE_FAST_PATH=0` and compare for yourself.

### The other levers

- `BROWSERUSE_MODEL=claude-sonnet-5` — 2.5x cheaper per token, and most browser
  steps are not hard reasoning. **Do not mix models within one task**: caches
  are model-scoped, so a per-step cascade forfeits cache reuse and can cost
  more than it saves.
- `BROWSERUSE_FAST_PATH_MAX=5` — longer autopilot runs, more saving, less
  oversight.
- Lower `BROWSERUSE_MAX_STEPS` to cap the worst case.
- Be specific. "Open google.com/travel/flights, Vienna to Lisbon, 18 Oct" costs
  a fraction of "book me a flight somewhere warm" — you are not paying the
  agent to explore.
- Watch the cache hit rate in `/cost`. Near zero means a prefix invalidator
  crept in and every turn is at full price.

Prices live in one table, `src/browseruse/agent/cost.py`, if they move.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

118 tests. The DOM and action tests drive a real headless Chromium against
`tests/fixtures/shop.html` and are skipped if no Chromium is installed; the Jev
tests run against a mocked API and assert the wire shapes (including that the
element `Choice` never exceeds 255 labels); the loop tests run the full
orchestration against a scripted Claude and a fake browser, covering the
approval gate, declined actions, Jev outages and stale-index recovery; and
`test_no_secret_leaks.py` asserts that no credential reaches either model, and
`test_cost.py` pins the billing arithmetic against published rates. The fast
path has its own tests for every guard: the confidence floors, the consecutive
cap, read-only mode, and that a risky step never autopilots past the gate.

## Known limits

- Claude is told to act one step at a time. If it ever batches two actions, the
  second is evaluated against the snapshot taken before the first ran — the
  index re-match catches most of that, but not all of it.


- Cross-origin iframes are not addressable. Same-origin ones are.
- Captchas are not solved, by design. It stops and hands the window back.
- The element list is viewport-biased: off-screen elements are listed but marked
  `(below the fold)` and need a scroll first.
