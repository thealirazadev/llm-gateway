import pytest

from app.services.redaction import Redactor, luhn_valid, restore


def test_checklist_prompt_redacts_three_kinds_and_leaves_the_lookalike():
    redactor = Redactor()
    text = (
        "I'm ana@example.com, call +1 415-555-0132 or (020) 7946 0958. "
        "Card 4111 1111 1111 1111, not 4111 1111 1111 1112."
    )

    redacted = redactor.redact(text)

    assert redacted == (
        "I'm <pii_email_1>, call <pii_phone_1> or <pii_phone_2>. "
        "Card <pii_card_1>, not 4111 1111 1111 1112."
    )
    kinds = {p.split("_")[1] for p in redactor.placeholders}
    assert kinds == {"email", "phone", "card"}
    assert redactor.count == 4
    assert restore(redacted, redactor.placeholders) == text


def test_identical_values_share_a_placeholder_across_messages():
    redactor = Redactor()
    first = redactor.redact("Write to bo@example.com")
    second = redactor.redact("Again: bo@example.com, or cy@example.com")

    assert first == "Write to <pii_email_1>"
    assert second == "Again: <pii_email_1>, or <pii_email_2>"
    assert redactor.count == 3


@pytest.mark.parametrize(
    "text",
    [
        "Order 12345678 shipped",
        "Released 2026-11-09 and 09/11/2026",
        "Version 1.2.3 at 10:30",
        "Price 1,299.00",
        "Server 192.168.1.1",
        "Tracking 4111111111111112",
    ],
)
def test_common_numbers_are_not_mistaken_for_pii(text):
    redactor = Redactor()
    assert redactor.redact(text) == text
    assert redactor.count == 0


@pytest.mark.parametrize("phone", ["555-0132", "415.555.0132", "+442079460958", "+44 20 7946 0958"])
def test_phone_formats(phone):
    assert Redactor().redact(f"call {phone} now") == "call <pii_phone_1> now"


def test_card_numbers_need_a_valid_checksum():
    assert luhn_valid("4111111111111111")
    assert not luhn_valid("4111111111111112")
    assert Redactor().redact("5500-0000-0000-0004") == "<pii_card_1>"


def test_restore_ignores_placeholders_the_model_invented():
    redactor = Redactor()
    redactor.redact("me@example.com")

    restored = restore("Hi <pii_email_1>, not <pii_email_2> or <pii_card_1>", redactor.placeholders)

    assert restored == "Hi me@example.com, not <pii_email_2> or <pii_card_1>"
