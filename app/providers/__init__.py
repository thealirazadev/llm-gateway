"""The adapter registry. A route's provider name resolves to exactly one adapter here."""

from app.providers.anthropic import PROVIDER as ANTHROPIC_PROVIDER
from app.providers.base import Provider
from app.providers.gemini import PROVIDER as GEMINI_PROVIDER
from app.providers.openai import PROVIDER as OPENAI_PROVIDER

PROVIDERS: dict[str, Provider] = {
    OPENAI_PROVIDER.name: OPENAI_PROVIDER,
    ANTHROPIC_PROVIDER.name: ANTHROPIC_PROVIDER,
    GEMINI_PROVIDER.name: GEMINI_PROVIDER,
}
