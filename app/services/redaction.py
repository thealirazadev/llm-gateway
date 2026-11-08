"""PII redaction: emails, phone numbers, and Luhn-valid card numbers become numbered
placeholders before a prompt leaves the gateway, and are restored in the response.

The placeholder map lives in request memory only (docs/architecture.md, "PII redaction").
"""

import re
from dataclasses import dataclass, field

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# 13-19 digits, optionally grouped by single spaces or hyphens.
_CARD = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
# E.164 or common separators: +44 20 7946 0958, (415) 555-0132, 415.555.0132, 555-0132.
_PHONE = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{1,4}\)[ .-]?)?\d{2,4}(?:[ .-]\d{2,4}){1,4}(?!\w)"
    r"|(?<![\w+])\+\d{7,15}(?!\w)"
)
_MIN_PHONE_DIGITS = 7
_DATE = re.compile(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[-./]\d{1,2}[-./]\d{2,4}")


def luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        n = int(char)
        if index % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


@dataclass
class Redactor:
    """Redacts the messages of one request; identical values share one placeholder."""

    placeholders: dict[str, str] = field(default_factory=dict)  # placeholder -> original
    _by_value: dict[tuple[str, str], str] = field(default_factory=dict)
    _counts: dict[str, int] = field(default_factory=dict)
    count: int = 0

    def _placeholder(self, kind: str, value: str) -> str:
        self.count += 1
        key = (kind, value)
        if key not in self._by_value:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            placeholder = f"<pii_{kind}_{self._counts[kind]}>"
            self._by_value[key] = placeholder
            self.placeholders[placeholder] = value
        return self._by_value[key]

    def redact(self, text: str) -> str:
        text = _EMAIL.sub(lambda m: self._placeholder("email", m.group(0)), text)

        def card(match: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", match.group(0))
            if luhn_valid(digits):
                return self._placeholder("card", match.group(0))
            return match.group(0)

        text = _CARD.sub(card, text)

        def phone(match: re.Match[str]) -> str:
            value = match.group(0)
            digits = re.sub(r"\D", "", value)
            # Long digit runs are card-shaped numbers that failed Luhn: leave them alone.
            if _MIN_PHONE_DIGITS <= len(digits) <= 15 and not _DATE.fullmatch(value):
                return self._placeholder("phone", value)
            return value

        return _PHONE.sub(phone, text)


def restore(text: str, placeholders: dict[str, str]) -> str:
    """Replace only this request's placeholders; invented look-alikes pass through untouched."""
    for placeholder, value in placeholders.items():
        text = text.replace(placeholder, value)
    return text
