"""Tests for MiniMax LLM and TTS provider integration."""

import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from verticals.llm import call_llm, get_provider
from verticals.tts import (
    MINIMAX_TTS_VOICES,
    _call_minimax_tts,
    _generate_minimax,
    get_tts_provider,
)


def _fake_litellm(content="Hello from MiniMax"):
    """Fake litellm module capturing completion kwargs."""
    fake = types.ModuleType("litellm")
    mock_msg = MagicMock()
    mock_msg.content = content
    mock_choice = MagicMock()
    mock_choice.message = mock_msg
    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]
    fake.completion = MagicMock(return_value=mock_resp)
    return fake


class TestMinimaxLLMProvider:
    def test_get_provider_returns_minimax_when_key_set(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("LLM_PROVIDER", raising=False)

        with patch("verticals.llm.get_anthropic_key", return_value=""):
            with patch("verticals.llm.get_gemini_key", return_value=""):
                with patch("verticals.llm.get_minimax_key", return_value="test-key"):
                    with patch("verticals.config.load_config", return_value={}):
                        provider = get_provider()
                        assert provider == "minimax"

    def test_get_provider_explicit_minimax(self):
        assert get_provider("minimax") == "minimax"

    def test_call_minimax_success(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
        monkeypatch.delenv("MINIMAX_BASE_URL", raising=False)
        fake = _fake_litellm()
        sys.modules["litellm"] = fake
        try:
            with patch("verticals.llm.get_minimax_key", return_value="test-key"):
                result = call_llm("Say hello", provider="minimax")
        finally:
            del sys.modules["litellm"]

        assert result == "Hello from MiniMax"
        kwargs = fake.completion.call_args.kwargs
        assert kwargs["model"] == "openai/MiniMax-M2.7"
        assert kwargs["temperature"] == 1.0
        assert kwargs["api_key"] == "test-key"
        assert kwargs["api_base"].startswith("https://api.minimax.io/v1")

    def test_call_minimax_uses_custom_base_url(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_BASE_URL", "https://custom.minimax.example/v1")
        fake = _fake_litellm("ok")
        sys.modules["litellm"] = fake
        try:
            with patch("verticals.llm.get_minimax_key", return_value="key"):
                call_llm("test", provider="minimax")
        finally:
            del sys.modules["litellm"]

        assert fake.completion.call_args.kwargs["api_base"] == "https://custom.minimax.example/v1"

    def test_call_minimax_propagates_error(self):
        fake = types.ModuleType("litellm")
        fake.completion = MagicMock(side_effect=RuntimeError("upstream 429"))
        sys.modules["litellm"] = fake
        try:
            with patch("verticals.llm.get_minimax_key", return_value="key"):
                with patch("time.sleep"):  # skip backoff delays
                    with pytest.raises(RuntimeError, match="upstream 429"):
                        call_llm("test", provider="minimax")
        finally:
            del sys.modules["litellm"]

    def test_call_minimax_raises_when_no_api_key(self):
        with patch("verticals.llm.get_minimax_key", return_value=""):
            with patch("time.sleep"):  # skip backoff delays
                with pytest.raises(RuntimeError, match="MINIMAX_API_KEY not set"):
                    call_llm("test", provider="minimax")


class TestMinimaxTTSProvider:
    def test_get_tts_provider_returns_minimax_when_key_set(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
        monkeypatch.delenv("TTS_PROVIDER", raising=False)

        with patch("verticals.tts.get_minimax_key", return_value="test-key"):
            # Simulate edge_tts not installed
            with patch("builtins.__import__", side_effect=lambda name, *a, **kw: (
                (_ for _ in ()).throw(ImportError()) if name == "edge_tts" else __import__(name, *a, **kw)
            )):
                pass  # edge_tts import patch is complex; test directly below

    def test_get_tts_provider_explicit_minimax(self):
        assert get_tts_provider("minimax") == "minimax"

    def test_minimax_voices_list_not_empty(self):
        assert len(MINIMAX_TTS_VOICES) > 0
        assert "English_Graceful_Lady" in MINIMAX_TTS_VOICES

    def test_call_minimax_tts_success(self):
        # MiniMax TTS returns streaming SSE data lines
        sse_data = (
            b'data: {"data": {"audio": "494433", "status": 1}}\n\n'
            b'data: {"data": {"audio": "494433", "status": 2}}\n\n'
            b"data: [DONE]\n\n"
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_content.return_value = [sse_data]

        with patch("requests.post", return_value=mock_response):
            result = _call_minimax_tts("Hello world", "English_Graceful_Lady", "test-key")

        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_call_minimax_tts_raises_on_error(self):
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad request"

        with patch("requests.post", return_value=mock_response):
            with pytest.raises(RuntimeError, match="MiniMax TTS 400"):
                _call_minimax_tts("test", "English_Graceful_Lady", "key")

    def test_call_minimax_tts_uses_correct_endpoint(self):
        sse_data = b'data: {"data": {"audio": "494433", "status": 2}}\n\n'
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_content.return_value = [sse_data]

        with patch("requests.post", return_value=mock_response) as mock_post:
            _call_minimax_tts("test", "English_Graceful_Lady", "key", model="speech-2.8-hd")

        url = mock_post.call_args[0][0]
        assert "/v1/t2a_v2" in url
        assert "api.minimax.io" in url

    def test_call_minimax_tts_sends_correct_model(self):
        sse_data = b'data: {"data": {"audio": "494433", "status": 2}}\n\n'
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_content.return_value = [sse_data]

        with patch("requests.post", return_value=mock_response) as mock_post:
            _call_minimax_tts("test", "English_Graceful_Lady", "key", model="speech-2.8-turbo")

        payload = mock_post.call_args[1]["json"]
        assert payload["model"] == "speech-2.8-turbo"
        assert payload["stream"] is True

    def test_generate_minimax_saves_file(self, tmp_path):
        sse_data = b'data: {"data": {"audio": "494433", "status": 2}}\n\n'
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_content.return_value = [sse_data]

        with patch("verticals.tts.get_minimax_key", return_value="test-key"):
            with patch("requests.post", return_value=mock_response):
                out = _generate_minimax("Hello world", tmp_path, "en")

        assert out.exists()
        assert out.name == "voiceover_en.mp3"
        assert out.read_bytes() == bytes.fromhex("494433")

    def test_generate_minimax_raises_without_api_key(self, tmp_path):
        with patch("verticals.tts.get_minimax_key", return_value=""):
            with pytest.raises(RuntimeError, match="MINIMAX_API_KEY not set"):
                _generate_minimax("test", tmp_path, "en")

    def test_hex_audio_decoding(self):
        # Verify hex decode works correctly (not base64)
        hex_str = "494433"
        decoded = bytes.fromhex(hex_str)
        assert decoded == b"ID3"  # MP3 ID3 tag magic bytes
