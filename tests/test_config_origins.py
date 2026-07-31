"""Tests for config.py's ALLOWED_ORIGINS setting and its parser
(_parse_allowed_origins). The parser is pure (no I/O, no Flask), so these
call it directly with sample strings rather than going through Flask or a
real settings.cfg file - see tests/test_config_secrets.py for that layer.
"""

import re

import config


def test_default_value_parses_to_localhost_any_port_regexes():
    patterns = config._parse_allowed_origins("http://localhost:*,http://127.0.0.1:*")
    assert len(patterns) == 2
    assert all(isinstance(p, re.Pattern) for p in patterns)

    localhost_pattern, loopback_pattern = patterns
    assert localhost_pattern.match("http://localhost:5000")
    assert localhost_pattern.match("http://localhost:3000")
    assert not localhost_pattern.match("http://localhost.evil.com:3000")
    assert not localhost_pattern.match("https://localhost:3000")

    assert loopback_pattern.match("http://127.0.0.1:8080")
    assert not loopback_pattern.match("http://127.0.0.1.evil.com:8080")


def test_literal_origin_passes_through_unmodified():
    patterns = config._parse_allowed_origins("https://stc.example.com")
    assert patterns == ["https://stc.example.com"]


def test_mixed_wildcard_and_literal_entries():
    patterns = config._parse_allowed_origins(
        "http://localhost:*,https://stc.example.com"
    )
    assert len(patterns) == 2
    assert isinstance(patterns[0], re.Pattern)
    assert patterns[0].match("http://localhost:9999")
    assert patterns[1] == "https://stc.example.com"


def test_whitespace_around_entries_is_trimmed():
    patterns = config._parse_allowed_origins(" http://localhost:* , https://stc.example.com ")
    assert len(patterns) == 2
    assert patterns[1] == "https://stc.example.com"


def test_wildcard_host_with_dot_is_escaped_not_treated_as_regex():
    # A literal "." in a wildcard-port entry must match only a literal ".",
    # not "any character" - otherwise "https://myXhost:*" would incorrectly
    # match "https://my.host:3000".
    patterns = config._parse_allowed_origins("https://my.host:*")
    pattern = patterns[0]
    assert isinstance(pattern, re.Pattern)
    assert pattern.match("https://my.host:3000")
    assert not pattern.match("https://myXhost:3000")


def test_config_module_exposes_allowed_origins():
    # config was already imported by conftest.py against the pytest
    # session's SC_TEST_DATA_DIR before this test file collected - see
    # tests/test_config_secrets.py's test_config_module_exposes_typed_settings
    # for why the whole suite shares one already-initialized singleton.
    assert isinstance(config.ALLOWED_ORIGINS, list)
    assert len(config.ALLOWED_ORIGINS) >= 1
