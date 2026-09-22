"""Interfaces for the speech layer, plus a no-op implementation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SpeechToText(Protocol):
    """Blocks until the user has said something, then returns the transcript."""

    async def listen(self) -> str: ...


@runtime_checkable
class TextToSpeech(Protocol):
    """Says ``text`` out loud. Should return once speaking has finished."""

    async def speak(self, text: str) -> None: ...


class NullTTS:
    """Says nothing. The default while the CLI is text-only."""

    async def speak(self, text: str) -> None:  # noqa: D102 - protocol impl
        return None
