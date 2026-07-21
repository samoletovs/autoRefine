"""Unit tests for agent.notify."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent import notify


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NAURO_BOT_TOKEN", raising=False)
    monkeypatch.delenv("NAURO_CHAT_ID", raising=False)


def test_skip_when_token_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAURO_CHAT_ID", "12345")
    assert notify.send_telegram("hi") is False


def test_skip_when_chat_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAURO_BOT_TOKEN", "abc")
    assert notify.send_telegram("hi") is False


def test_send_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAURO_BOT_TOKEN", "tok")
    monkeypatch.setenv("NAURO_CHAT_ID", "999")

    mock_response = MagicMock()
    mock_response.status_code = 200

    with patch("agent.notify.httpx.Client") as mock_client_cls:
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.post.return_value = mock_response

        result = notify.send_telegram("hello world", parse_mode="HTML")

    assert result is True
    client_instance.post.assert_called_once()
    args, kwargs = client_instance.post.call_args
    assert args[0] == "https://api.telegram.org/bottok/sendMessage"
    assert kwargs["json"]["chat_id"] == "999"
    assert kwargs["json"]["text"] == "hello world"
    assert kwargs["json"]["parse_mode"] == "HTML"


def test_send_retries_on_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAURO_BOT_TOKEN", "tok")
    monkeypatch.setenv("NAURO_CHAT_ID", "999")

    fail = MagicMock(status_code=503, text="busy")
    ok = MagicMock(status_code=200)

    with patch("agent.notify.httpx.Client") as mock_client_cls:
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.post.side_effect = [fail, ok]

        result = notify.send_telegram("hi")

    assert result is True
    assert client_instance.post.call_count == 2


def test_send_fails_on_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAURO_BOT_TOKEN", "tok")
    monkeypatch.setenv("NAURO_CHAT_ID", "999")

    with patch("agent.notify.httpx.Client") as mock_client_cls:
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.post.return_value = MagicMock(status_code=400, text="bad")

        result = notify.send_telegram("hi")

    assert result is False
    assert client_instance.post.call_count == 1


def test_truncates_long_messages() -> None:
    msg = "x" * (notify.TELEGRAM_MAX_LEN + 500)
    truncated = notify._truncate(msg)
    assert len(truncated) <= notify.TELEGRAM_MAX_LEN
    assert truncated.endswith("[truncated]")


def test_short_messages_not_truncated() -> None:
    msg = "short"
    assert notify._truncate(msg) == "short"


def test_explicit_args_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAURO_BOT_TOKEN", "from_env")
    monkeypatch.setenv("NAURO_CHAT_ID", "111")

    with patch("agent.notify.httpx.Client") as mock_client_cls:
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.post.return_value = MagicMock(status_code=200)

        notify.send_telegram("hi", bot_token="explicit", chat_id="222")

    args, kwargs = client_instance.post.call_args
    assert "explicit" in args[0]
    assert kwargs["json"]["chat_id"] == "222"


def test_network_error_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAURO_BOT_TOKEN", "tok")
    monkeypatch.setenv("NAURO_CHAT_ID", "999")

    import httpx as _httpx

    with patch("agent.notify.httpx.Client") as mock_client_cls:
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.post.side_effect = _httpx.ConnectError("dns")

        assert notify.send_telegram("hi") is False


# --- send_idea_card ---------------------------------------------------------


def test_idea_card_skipped_without_creds() -> None:
    assert notify.send_idea_card("samoletovs/era", 12, "Add CSV export") is False


def test_idea_card_encodes_buttons_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAURO_BOT_TOKEN", "tok")
    monkeypatch.setenv("NAURO_CHAT_ID", "999")

    with patch("agent.notify.httpx.Client") as mock_client_cls:
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.post.return_value = MagicMock(status_code=200)

        result = notify.send_idea_card(
            "samoletovs/era", 12, "Add CSV export", priority="P1", description="Nice to have"
        )

    assert result is True
    _, kwargs = client_instance.post.call_args
    payload = kwargs["json"]
    # Bare repo name is encoded (nauroBot prepends the owner).
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "arf:era:12:y"
    assert buttons[1]["callback_data"] == "arf:era:12:n"
    # The card text carries the arf token so a reply can be attributed to the issue.
    assert "arf:era:12" in payload["text"]
    assert "Add CSV export" in payload["text"]


def test_idea_card_callback_data_within_telegram_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAURO_BOT_TOKEN", "tok")
    monkeypatch.setenv("NAURO_CHAT_ID", "999")

    with patch("agent.notify.httpx.Client") as mock_client_cls:
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.post.return_value = MagicMock(status_code=200)

        notify.send_idea_card("samoletovs/portaBaltica", 99999, "x" * 200)

    _, kwargs = client_instance.post.call_args
    for button in kwargs["json"]["reply_markup"]["inline_keyboard"][0]:
        assert len(button["callback_data"].encode("utf-8")) <= 64
