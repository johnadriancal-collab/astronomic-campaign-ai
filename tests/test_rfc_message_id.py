"""app/services/rfc_message_id.py -- pure, zero-dependency generator."""

import re

from app.services.rfc_message_id import generate_rfc_message_id


def test_message_id_has_local_part_at_domain_shape_with_no_angle_brackets():
    mid = generate_rfc_message_id("astronomic.com")
    assert mid.endswith("@astronomic.com")
    assert "<" not in mid and ">" not in mid
    local_part = mid.split("@", 1)[0]
    assert re.fullmatch(r"[0-9a-f]{32}", local_part)  # uuid4().hex


def test_message_id_is_unique_across_calls():
    ids = {generate_rfc_message_id("astronomic.com") for _ in range(500)}
    assert len(ids) == 500


def test_message_id_uses_the_given_domain_verbatim():
    assert generate_rfc_message_id("customdomain.io").endswith("@customdomain.io")
