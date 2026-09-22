"""Tool schemas handed to Claude.

The order of this list is frozen on purpose: tools are rendered before the
system prompt and the messages, so re-ordering them would invalidate the prompt
cache on every request.
"""

from __future__ import annotations

from typing import Any

_INDEX = {
    "type": "integer",
    "description": "The [n] number of the element from the current element list.",
}
_TARGET = {
    "type": "string",
    "description": (
        "A short description of the element you mean, e.g. 'the blue Search button'. "
        "Always include this: if the page re-renders and the index shifts, Jev uses "
        "this description to find the element again instead of clicking the wrong thing."
    ),
}

BROWSER_TOOLS: list[dict[str, Any]] = [
    {
        "name": "navigate",
        "description": "Open a URL in the current tab.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "click",
        "description": "Click an element from the current element list.",
        "input_schema": {
            "type": "object",
            "properties": {"index": _INDEX, "target_description": _TARGET},
            "required": ["index", "target_description"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "type_text",
        "description": (
            "Type into a text field. Set press_enter when the field is a search box "
            "or the form submits on Enter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "index": _INDEX,
                "target_description": _TARGET,
                "text": {"type": "string"},
                "press_enter": {"type": "boolean"},
                "clear_first": {"type": "boolean"},
            },
            "required": ["index", "target_description", "text", "press_enter", "clear_first"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "select_option",
        "description": "Choose an option in a <select> dropdown by its visible label.",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": _INDEX,
                "target_description": _TARGET,
                "value": {"type": "string", "description": "The option's visible text."},
            },
            "required": ["index", "target_description", "value"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "scroll",
        "description": "Scroll the page. Elements marked '(below the fold)' need this first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"]},
                "amount": {"type": "integer", "description": "Pixels, default 600."},
            },
            "required": ["direction"],
            "additionalProperties": False,
        },
    },
    {
        "name": "scroll_to_text",
        "description": "Scroll until some visible text comes into view.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "press_key",
        "description": "Press a key, e.g. Enter, Escape, Tab, PageDown.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "go_back",
        "description": "Go back one entry in the tab's history.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "extract_text",
        "description": (
            "Re-read the full visible text of the page. Use this when you need to "
            "quote or summarise content rather than act on it."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "new_tab",
        "description": "Open a new tab, optionally at a URL, and switch to it.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "switch_tab",
        "description": "Switch to an open tab by its number from list_tabs.",
        "input_schema": {
            "type": "object",
            "properties": {"index": {"type": "integer"}},
            "required": ["index"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "list_tabs",
        "description": "List the open tabs with their numbers, titles and URLs.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "wait",
        "description": "Wait for the page to catch up. Max 15 seconds.",
        "input_schema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "done",
        "description": (
            "Finish the task and report back. Call this as soon as the goal is met, "
            "or when you cannot proceed and need the user to step in."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "What you did and what the user should know, in plain language.",
                }
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]

TOOL_NAMES = frozenset(tool["name"] for tool in BROWSER_TOOLS)
