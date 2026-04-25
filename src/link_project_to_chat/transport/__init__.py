"""Transport abstraction — see docs/superpowers/specs/2026-04-20-transport-abstraction-design.md."""
from .base import (
    AuthorizerCallback,
    Button,
    ButtonClick,
    ButtonHandler,
    ButtonStyle,
    Buttons,
    ChatKind,
    ChatRef,
    CommandHandler,
    CommandInvocation,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageHandler,
    MessageRef,
    OnReadyCallback,
    PromptHandler,
    PromptKind,
    PromptOption,
    PromptRef,
    PromptSpec,
    PromptSubmission,
    Transport,
    TransportRetryAfter,
)
from .fake import ClosedPrompt, EditedMessage, FakeTransport, OpenedPrompt, SentFile, SentMessage, SentVoice
from .telegram import TelegramTransport

# WebTransport is optional — depends on the `web` extra (FastAPI, uvicorn, aiosqlite).
try:
    from ..web.transport import WebTransport  # noqa: F401
    _WEB_AVAILABLE = True
except ImportError:
    _WEB_AVAILABLE = False

__all__ = [
    "AuthorizerCallback",
    "Button",
    "ButtonClick",
    "ButtonHandler",
    "ButtonStyle",
    "Buttons",
    "ChatKind",
    "ChatRef",
    "ClosedPrompt",
    "CommandHandler",
    "CommandInvocation",
    "EditedMessage",
    "FakeTransport",
    "Identity",
    "IncomingFile",
    "IncomingMessage",
    "MessageHandler",
    "MessageRef",
    "OnReadyCallback",
    "OpenedPrompt",
    "PromptHandler",
    "PromptKind",
    "PromptOption",
    "PromptRef",
    "PromptSpec",
    "PromptSubmission",
    "SentFile",
    "SentMessage",
    "SentVoice",
    "TelegramTransport",
    "Transport",
    "TransportRetryAfter",
]

if _WEB_AVAILABLE:
    __all__.append("WebTransport")
