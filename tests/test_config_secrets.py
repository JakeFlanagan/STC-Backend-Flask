"""Tests for config.py's settings.cfg loader/generator
(_load_or_create_settings). These call the function directly with a
tmp_path, independent of the already-initialized `config` singleton that
tests/conftest.py sets up for the rest of the suite - see that module's
docstring for why the singleton itself isn't touched here.
"""

import config


def test_missing_file_is_created_fully_populated(tmp_path, capsys):
    settings_path = tmp_path / "settings.cfg"

    values = config._load_or_create_settings(settings_path)

    assert settings_path.exists()
    # Deliberately blank by default - each is an opt-in integration (Redis
    # rate-limit storage, Sentry error tracking, SMTP email sending) and
    # not one of _GENERATED_KEYS - see each one's comment in config.py.
    _blank_by_default = {"REDIS_URL", "SENTRY_DSN", "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM"}
    for key in config._DEFAULTS:
        if key in _blank_by_default:
            continue
        assert values[key], f"{key} should not be blank"
    for key in _blank_by_default:
        assert values[key] == ""
    assert values["ADMIN_USERNAME"] == "admin"

    out = capsys.readouterr().out
    assert "Admin created" in out
    assert values["ADMIN_PASSWORD"] in out


def test_existing_file_with_blank_field_is_filled_in(tmp_path, capsys):
    settings_path = tmp_path / "settings.cfg"
    settings_path.write_text(
        "SC_HOST=0.0.0.0\n"
        "SC_PORT=9000\n"
        "SC_DEBUG=0\n"
        "JWT_SECRET=already-set\n"
        "JWT_EXPIRY_MINUTES=30\n"
        "DB_ENCRYPTION_KEY=\n"
        "ADMIN_USERNAME=root\n"
        "ADMIN_PASSWORD=already-set\n"
    )

    values = config._load_or_create_settings(settings_path)

    assert values["DB_ENCRYPTION_KEY"], "blank field should be generated"
    # Untouched fields keep their exact original values.
    assert values["SC_HOST"] == "0.0.0.0"
    assert values["SC_PORT"] == "9000"
    assert values["JWT_SECRET"] == "already-set"
    assert values["ADMIN_USERNAME"] == "root"
    assert values["ADMIN_PASSWORD"] == "already-set"

    # The on-disk file's untouched lines are byte-identical to what was
    # written above - set_key's in-place edit only rewrote the one blank
    # DB_ENCRYPTION_KEY field.
    on_disk = settings_path.read_text()
    assert "SC_HOST=0.0.0.0" in on_disk
    assert "SC_PORT=9000" in on_disk
    assert "SC_DEBUG=0" in on_disk
    assert "JWT_SECRET=already-set" in on_disk
    assert "JWT_EXPIRY_MINUTES=30" in on_disk
    assert "ADMIN_USERNAME=root" in on_disk
    assert "ADMIN_PASSWORD=already-set" in on_disk

    out = capsys.readouterr().out
    assert "Admin created" not in out, "no new admin password was generated"


def test_fully_populated_file_is_left_untouched(tmp_path, capsys):
    settings_path = tmp_path / "settings.cfg"
    original_text = (
        "SC_HOST=0.0.0.0\n"
        "SC_PORT=9000\n"
        "SC_DEBUG=1\n"
        "JWT_SECRET=abc123\n"
        "JWT_EXPIRY_MINUTES=30\n"
        "DB_ENCRYPTION_KEY=def456\n"
        "ADMIN_USERNAME=root\n"
        "ADMIN_PASSWORD=hunter2\n"
    )
    settings_path.write_text(original_text)

    values = config._load_or_create_settings(settings_path)

    assert settings_path.read_text() == original_text
    assert values["DB_ENCRYPTION_KEY"] == "def456"

    out = capsys.readouterr().out
    assert out == ""


def test_config_module_exposes_typed_settings():
    # config was already imported (by conftest.py, before this test file
    # even collected) against the pytest session's SC_TEST_DATA_DIR, so
    # this checks the already-initialized singleton rather than re-running
    # _load_or_create_settings - see conftest.py's docstring for why the
    # whole suite shares one BASE_DIR.
    assert config.SETTINGS_FILE.exists()
    assert isinstance(config.SC_HOST, str) and config.SC_HOST
    assert isinstance(config.SC_PORT, int)
    assert isinstance(config.SC_DEBUG, bool)
    assert isinstance(config.JWT_SECRET, str) and config.JWT_SECRET
    assert isinstance(config.JWT_EXPIRY_MINUTES, int)
    assert isinstance(config.DB_ENCRYPTION_KEY, str) and config.DB_ENCRYPTION_KEY
    assert isinstance(config.ADMIN_USERNAME, str) and config.ADMIN_USERNAME
    assert isinstance(config.ADMIN_PASSWORD, str) and config.ADMIN_PASSWORD


def test_resolve_redis_url_prefers_env_var_over_settings(monkeypatch):
    monkeypatch.setenv("SC_REDIS_URL", "redis://from-env:6379/0")
    result = config._resolve_redis_url({"REDIS_URL": "redis://from-settings:6379/0"})
    assert result == "redis://from-env:6379/0"


def test_resolve_redis_url_falls_back_to_settings_when_env_unset(monkeypatch):
    monkeypatch.delenv("SC_REDIS_URL", raising=False)
    result = config._resolve_redis_url({"REDIS_URL": "redis://from-settings:6379/0"})
    assert result == "redis://from-settings:6379/0"


def test_resolve_redis_url_falls_back_when_env_var_is_blank(monkeypatch):
    monkeypatch.setenv("SC_REDIS_URL", "")
    result = config._resolve_redis_url({"REDIS_URL": "redis://from-settings:6379/0"})
    assert result == "redis://from-settings:6379/0"


def test_bootstrapped_flag_stops_a_blank_admin_password_being_regenerated(tmp_path, capsys):
    """Once the admin account exists, a blank ADMIN_PASSWORD means "no
    longer needed", not "never generated". Without the flag, clearing the
    plaintext would mint a replacement on the next start-up and announce
    the creation of an account that already existed."""
    settings_path = tmp_path / "settings.cfg"
    settings_path.write_text(
        "JWT_SECRET=already-set\n"
        "DB_ENCRYPTION_KEY=already-set\n"
        "ADMIN_USERNAME=root\n"
        "ADMIN_PASSWORD=\n"
        "ADMIN_BOOTSTRAPPED=1\n"
    )

    values = config._load_or_create_settings(settings_path)

    assert values["ADMIN_PASSWORD"] == "", "a bootstrapped admin password must stay blank"
    assert "ADMIN_PASSWORD=\n" in settings_path.read_text(), "nothing should have been written back"
    assert "Admin created" not in capsys.readouterr().out


def test_blank_admin_password_is_still_generated_before_bootstrap(tmp_path, capsys):
    """The mirror image of the test above: with the flag at its 0 default,
    a blank password is a genuinely missing one and must be generated, or a
    fresh install would have no way to log in at all."""
    settings_path = tmp_path / "settings.cfg"
    settings_path.write_text(
        "JWT_SECRET=already-set\n"
        "DB_ENCRYPTION_KEY=already-set\n"
        "ADMIN_USERNAME=root\n"
        "ADMIN_PASSWORD=\n"
        "ADMIN_BOOTSTRAPPED=0\n"
    )

    values = config._load_or_create_settings(settings_path)

    assert values["ADMIN_PASSWORD"], "a pre-bootstrap blank password must be generated"
    assert "Admin created" in capsys.readouterr().out
