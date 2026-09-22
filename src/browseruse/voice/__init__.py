"""Speech in and out.

Nothing here talks to a microphone yet -- the CLI is text-only for now. What
this module does provide is the shape a backend has to fit, so adding local
Whisper (or any hosted STT) later is a matter of writing one class and passing
it to the CLI, not of rewriting the loop.

    class WhisperSTT:
        async def listen(self) -> str: ...

    class SayTTS:
        async def speak(self, text: str) -> None: ...
"""

from browseruse.voice.base import NullTTS, SpeechToText, TextToSpeech

__all__ = ["NullTTS", "SpeechToText", "TextToSpeech"]
