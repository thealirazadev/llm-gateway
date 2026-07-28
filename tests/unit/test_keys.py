import hashlib
import time

from app.auth import KEY_BODY_CHARS, KEY_PREFIX, generate_key, hash_key
from app.logging import new_request_id


def test_generated_key_has_the_documented_shape() -> None:
    raw_key, key_hash, last4 = generate_key()
    assert raw_key.startswith(KEY_PREFIX)
    assert len(raw_key) == len(KEY_PREFIX) + KEY_BODY_CHARS
    assert raw_key[len(KEY_PREFIX) :].isalnum()
    assert key_hash == hashlib.sha256(raw_key.encode()).hexdigest()
    assert last4 == raw_key[-4:]


def test_generated_keys_are_unique() -> None:
    keys = {generate_key()[0] for _ in range(50)}
    assert len(keys) == 50


def test_hash_key_is_stable() -> None:
    assert hash_key("lgw_example") == hash_key("lgw_example")
    assert hash_key("lgw_example") != hash_key("lgw_other")


def test_request_ids_are_26_chars_and_time_ordered() -> None:
    first = new_request_id()
    # Ordering comes from the millisecond timestamp prefix; ids minted inside one
    # millisecond differ only in their random suffix and have no defined order.
    time.sleep(0.002)
    second = new_request_id()
    assert len(first) == 26
    assert set(first) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    assert first < second


def test_request_ids_are_unique() -> None:
    assert len({new_request_id() for _ in range(200)}) == 200
