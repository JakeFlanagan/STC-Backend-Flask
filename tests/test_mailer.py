"""Tests for mailer.py's password-reset email sending. Never touches a
real SMTP server - config.SMTP_HOST is blank by default (the log-only
path), and the "real sending" path monkeypatches smtplib.SMTP itself.
"""

import smtplib

import config
import mailer
from helpers import expect


def test_send_password_reset_email_logs_instead_of_sending_when_smtp_host_blank(monkeypatch, caplog):
    monkeypatch.setattr(config, "SMTP_HOST", "")
    with caplog.at_level("INFO", logger="stc.mailer"):
        mailer.send_password_reset_email("user@example.com", "https://example.com/reset?token=abc")
    matching = [r for r in caplog.records if r.name == "stc.mailer"]
    expect("exactly one log record", 1, len(matching))
    expect("reset link included in the log record", "https://example.com/reset?token=abc", matching[0].reset_link)


class _FakeSMTP:
    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.started_tls = False
        self.logged_in_as = None
        self.sent_message = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.logged_in_as = (username, password)

    def send_message(self, message):
        self.sent_message = message


def test_send_password_reset_email_sends_via_smtp_when_host_set(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_PORT", 587)
    monkeypatch.setattr(config, "SMTP_USERNAME", "bot")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "hunter2")
    monkeypatch.setattr(config, "SMTP_FROM", "noreply@example.com")
    monkeypatch.setattr(config, "SMTP_USE_TLS", True)
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    mailer.send_password_reset_email("user@example.com", "https://example.com/reset?token=abc")

    expect("one SMTP connection made", 1, len(_FakeSMTP.instances))
    smtp = _FakeSMTP.instances[0]
    expect("starttls called (SMTP_USE_TLS=True)", True, smtp.started_tls)
    expect("logged in with configured credentials", ("bot", "hunter2"), smtp.logged_in_as)
    expect("message was sent", True, smtp.sent_message is not None)
    expect("recipient set", "user@example.com", smtp.sent_message["To"])
    expect("reset link present in body", True, "https://example.com/reset?token=abc" in smtp.sent_message.get_content())


def test_send_password_reset_email_swallows_smtp_errors(monkeypatch, caplog):
    class _RaisingSMTP(_FakeSMTP):
        def send_message(self, message):
            raise smtplib.SMTPException("connection refused")

    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_USERNAME", "")
    monkeypatch.setattr(smtplib, "SMTP", _RaisingSMTP)

    with caplog.at_level("ERROR", logger="stc.mailer"):
        mailer.send_password_reset_email("user@example.com", "https://example.com/reset?token=abc")  # must not raise

    matching = [r for r in caplog.records if r.name == "stc.mailer"]
    expect("failure logged, not raised", 1, len(matching))
