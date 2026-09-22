"""Turn a live page into a numbered list of things you can act on.

Neither model gets raw HTML. They get a compact index -- ``[12] button "Add to
basket"`` -- and every action refers to a number. That keeps the prompt small,
and it maps exactly onto Jev's Choice primitive, which takes up to 255 labels:
the element list *is* the choice set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Frame, Page

from browseruse.config import MAX_INDEXED_ELEMENTS

#: Marks an element so an action can find it again after extraction.
INDEX_ATTRIBUTE = "data-bu-idx"

_EXTRACT_JS = """
(args) => {
  const { startIndex, attribute, limit } = args;
  const SELECTOR = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'summary', 'label',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]',
    '[role=tab]', '[role=menuitem]', '[role=option]', '[role=switch]',
    '[role=searchbox]', '[role=textbox]', '[role=combobox]',
    '[contenteditable=""]', '[contenteditable="true"]',
    '[onclick]', '[tabindex]:not([tabindex="-1"])',
  ].join(',');

  for (const stale of document.querySelectorAll('[' + attribute + ']')) {
    stale.removeAttribute(attribute);
  }

  // Fields whose contents must never reach a model. Detected three ways,
  // because sites label these inconsistently: the input type, the
  // autocomplete hint the browser itself uses to autofill, and the name /
  // id / placeholder text.
  const SENSITIVE_AUTOCOMPLETE =
    /cc-number|cc-csc|cc-exp|current-password|new-password|one-time-code/i;
  const SENSITIVE_HINT =
    /pass|pwd|card.?num|cardnumber|cvv|cvc|ccv|secur|iban|sort.?code|account.?number|routing|ssn|social.?security|otp|2fa|mfa|totp|token|secret|\\bpin\\b|seed|mnemonic|private.?key/i;

  const isSensitive = (el) => {
    if (el.type === 'password') return true;
    if (SENSITIVE_AUTOCOMPLETE.test(el.getAttribute('autocomplete') || '')) return true;
    const hints = [
      el.getAttribute('name'), el.id,
      el.getAttribute('placeholder'), el.getAttribute('aria-label'),
    ].filter(Boolean).join(' ');
    return SENSITIVE_HINT.test(hints);
  };

  const isVisible = (el, rect) => {
    if (rect.width < 2 || rect.height < 2) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    if (parseFloat(style.opacity || '1') < 0.05) return false;
    if (el.disabled === true) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    return true;
  };

  // Skip elements sitting underneath an overlay: whatever is painted at the
  // element's centre is what a real click would hit.
  const isReachable = (el, rect) => {
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) {
      return 'offscreen';
    }
    const hit = document.elementFromPoint(x, y);
    if (!hit) return 'covered';
    return (hit === el || el.contains(hit) || hit.contains(el)) ? 'hit' : 'covered';
  };

  const describe = (el, sensitive) => {
    const pick = (...values) => {
      for (const value of values) {
        if (typeof value === 'string' && value.trim()) return value.trim();
      }
      return '';
    };
    let label = pick(
      el.getAttribute('aria-label'),
      el.getAttribute('placeholder'),
      el.getAttribute('title'),
      el.getAttribute('alt'),
      (el.innerText || '').slice(0, 200),
      sensitive ? '' : el.value,
      el.getAttribute('name'),
      el.getAttribute('aria-labelledby') &&
        (document.getElementById(el.getAttribute('aria-labelledby')) || {}).innerText,
    );
    return label.replace(/\\s+/g, ' ').slice(0, 140);
  };

  const results = [];
  let index = startIndex;
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (results.length >= limit) break;
    const rect = el.getBoundingClientRect();
    if (!isVisible(el, rect)) continue;
    const reach = isReachable(el, rect);
    if (reach === 'covered') continue;

    const sensitive = isSensitive(el);
    el.setAttribute(attribute, String(index));
    results.push({
      index,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      role: el.getAttribute('role') || '',
      label: describe(el, sensitive),
      // A sensitive field's contents never leave the page: the agent is told
      // the field exists and whether it is filled, and nothing more.
      value: (!sensitive && typeof el.value === 'string') ? el.value.slice(0, 120) : '',
      sensitive,
      filled: typeof el.value === 'string' && el.value.length > 0,
      href: (el.getAttribute('href') || '').slice(0, 300),
      checked: el.checked === true,
      in_viewport: reach === 'hit',
      box: {
        x: Math.round(rect.left), y: Math.round(rect.top),
        w: Math.round(rect.width), h: Math.round(rect.height),
      },
    });
    index += 1;
  }
  return results;
}
"""

# innerText on the live body is what the user actually sees: it honours
# display/visibility and already leaves out script and style content. Cloning
# the node first would detach it from the layout and leak hidden text back in.
_PAGE_TEXT_JS = """
(limit) => {
  if (!document.body) return '';
  return (document.body.innerText || '')
    .replace(/[ \\t]+/g, ' ')
    .replace(/\\n{3,}/g, '\\n\\n')
    .trim()
    .slice(0, limit);
}
"""


@dataclass(frozen=True)
class Element:
    """One thing on the page the agent can act on."""

    index: int
    tag: str
    label: str
    frame_url: str
    type: str = ""
    role: str = ""
    value: str = ""
    href: str = ""
    checked: bool = False
    in_viewport: bool = True
    #: A password, card or one-time-code field. Its contents are never captured.
    sensitive: bool = False
    #: Whether a sensitive field already has something in it.
    filled: bool = False

    def describe(self) -> str:
        """One line, short enough that 255 of them still fit in a prompt."""
        kind = self.role or (f"{self.tag}:{self.type}" if self.type else self.tag)
        parts = [f"[{self.index}]", kind]
        if self.label:
            parts.append(f'"{self.label}"')
        if self.sensitive:
            parts.append("(sensitive field, already filled)" if self.filled else "(sensitive field, empty)")
        elif self.value and self.value != self.label:
            parts.append(f"(value: {self.value})")
        if self.checked:
            parts.append("(checked)")
        if self.href and not self.href.startswith("javascript:"):
            parts.append(f"-> {self.href[:60]}")
        if not self.in_viewport:
            parts.append("(below the fold)")
        return " ".join(parts)


@dataclass
class PageSnapshot:
    """What the agent sees at one moment: URL, title, elements, visible text."""

    url: str
    title: str
    elements: list[Element]
    text: str
    truncated: bool = False
    frames: dict[int, Frame] = field(default_factory=dict, repr=False)

    def element(self, index: int) -> Element | None:
        return next((el for el in self.elements if el.index == index), None)

    def frame_for(self, index: int) -> Frame | None:
        return self.frames.get(index)

    def render(self, include_text: bool = True) -> str:
        """The text block handed to Claude each step.

        ``include_text`` drops the prose, which is the bulk of the tokens. The
        agent is told it was withheld rather than left to think the page is
        empty, and ``extract_text`` fetches it on demand.
        """
        lines = [f"URL: {self.url}", f"Title: {self.title}", "", "Interactive elements:"]
        lines.extend(f"  {el.describe()}" for el in self.elements)
        if not self.elements:
            lines.append("  (none found -- the page may still be loading)")
        if self.truncated:
            lines.append(
                f"  ... list capped at {MAX_INDEXED_ELEMENTS} elements; scroll to reach the rest"
            )
        if self.text and include_text:
            lines += ["", "Visible text:", self.text]
        elif self.text:
            lines += [
                "",
                f"Visible text: withheld ({len(self.text)} characters). This page looks "
                f"navigational; call extract_text if you need to read it.",
            ]
        return "\n".join(lines)

    def choice_labels(self) -> dict[str, str]:
        """The element list shaped as Jev Choice criteria."""
        return {str(el.index): el.describe() for el in self.elements}


async def snapshot(page: Page, *, text_limit: int = 4000) -> PageSnapshot:
    """Index every interactive element on ``page`` and its same-origin frames."""
    elements: list[Element] = []
    frames: dict[int, Frame] = {}
    next_index = 0

    for frame in page.frames:
        remaining = MAX_INDEXED_ELEMENTS - len(elements)
        if remaining <= 0:
            break
        try:
            raw: list[dict[str, Any]] = await frame.evaluate(
                _EXTRACT_JS,
                {"startIndex": next_index, "attribute": INDEX_ATTRIBUTE, "limit": remaining},
            )
        except Exception:
            # Cross-origin frames and frames that navigate mid-extraction are
            # simply not addressable; the rest of the page still is.
            continue
        for item in raw:
            elements.append(
                Element(
                    index=item["index"],
                    tag=item["tag"],
                    label=item["label"],
                    frame_url=frame.url,
                    type=item["type"],
                    role=item["role"],
                    value=item["value"],
                    href=item["href"],
                    checked=item["checked"],
                    in_viewport=item["in_viewport"],
                    sensitive=item["sensitive"],
                    filled=item["filled"],
                )
            )
            frames[item["index"]] = frame
        next_index += len(raw)

    try:
        text = await page.evaluate(_PAGE_TEXT_JS, text_limit)
    except Exception:
        text = ""

    return PageSnapshot(
        url=page.url,
        title=await page.title(),
        elements=elements,
        text=text,
        truncated=len(elements) >= MAX_INDEXED_ELEMENTS,
        frames=frames,
    )
