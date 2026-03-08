"""Tests for watch_token / unwatch_token support in hub and subscriber."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coinjure.hub.hub import MarketDataHub
from coinjure.hub.subscriber import HubDataSource

# ---------------------------------------------------------------------------
# HubDataSource — watch_token / unwatch_token don't crash without a hub
# ---------------------------------------------------------------------------


class TestHubDataSourceWatchToken:
    """HubDataSource.watch_token / unwatch_token should not raise even
    when no event loop is running or no hub is reachable."""

    def test_watch_token_no_loop(self, tmp_path: Path) -> None:
        """Calling watch_token outside an event loop should silently do nothing."""
        src = HubDataSource(tmp_path / 'hub.sock')
        # Should not raise
        src.watch_token('tok_123')

    def test_unwatch_token_no_loop(self, tmp_path: Path) -> None:
        """Calling unwatch_token outside an event loop should silently do nothing."""
        src = HubDataSource(tmp_path / 'hub.sock')
        # Should not raise
        src.unwatch_token('tok_123')

    @pytest.mark.asyncio
    async def test_watch_token_with_loop_no_hub(self, tmp_path: Path) -> None:
        """Calling watch_token inside an event loop (but no hub) should not raise."""
        src = HubDataSource(tmp_path / 'hub.sock')
        # The fire-and-forget task will fail to connect, but that's OK
        src.watch_token('tok_abc')
        # Give the fire-and-forget task a moment to attempt (and fail quietly)
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_unwatch_token_with_loop_no_hub(self, tmp_path: Path) -> None:
        """Calling unwatch_token inside an event loop (but no hub) should not raise."""
        src = HubDataSource(tmp_path / 'hub.sock')
        src.unwatch_token('tok_abc')
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# MarketDataHub._handle_control — watch_token / unwatch_token commands
# ---------------------------------------------------------------------------


class TestHubControlWatchToken:
    """MarketDataHub._handle_control should dispatch watch_token/unwatch_token
    to the underlying data source."""

    @pytest.fixture
    def mock_source(self) -> MagicMock:
        source = MagicMock()
        source.watch_token = MagicMock()
        source.unwatch_token = MagicMock()
        return source

    @pytest.fixture
    def hub(self, tmp_path: Path, mock_source: MagicMock) -> MarketDataHub:
        return MarketDataHub(tmp_path / 'hub.sock', mock_source)

    @pytest.mark.asyncio
    async def test_watch_token_command(
        self, hub: MarketDataHub, mock_source: MagicMock
    ) -> None:
        payload = json.dumps({'cmd': 'watch_token', 'token_id': 'tok_42'}) + '\n'
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        await hub._handle_control(payload.encode(), writer)

        mock_source.watch_token.assert_called_once_with('tok_42')
        # Should have written a JSON response with ok=True
        written = writer.write.call_args[0][0]
        resp = json.loads(written.decode())
        assert resp['ok'] is True

    @pytest.mark.asyncio
    async def test_unwatch_token_command(
        self, hub: MarketDataHub, mock_source: MagicMock
    ) -> None:
        payload = json.dumps({'cmd': 'unwatch_token', 'token_id': 'tok_42'}) + '\n'
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        await hub._handle_control(payload.encode(), writer)

        mock_source.unwatch_token.assert_called_once_with('tok_42')
        written = writer.write.call_args[0][0]
        resp = json.loads(written.decode())
        assert resp['ok'] is True

    @pytest.mark.asyncio
    async def test_watch_token_missing_token_id(
        self, hub: MarketDataHub, mock_source: MagicMock
    ) -> None:
        payload = json.dumps({'cmd': 'watch_token'}) + '\n'
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        await hub._handle_control(payload.encode(), writer)

        mock_source.watch_token.assert_not_called()
        written = writer.write.call_args[0][0]
        resp = json.loads(written.decode())
        assert resp['ok'] is False
        assert 'token_id required' in resp['error']

    @pytest.mark.asyncio
    async def test_watch_token_source_without_method(self, tmp_path: Path) -> None:
        """If the underlying source has no watch_token, the command still succeeds."""
        source = MagicMock(spec=[])  # no attributes
        hub = MarketDataHub(tmp_path / 'hub.sock', source)

        payload = json.dumps({'cmd': 'watch_token', 'token_id': 'tok_99'}) + '\n'
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        await hub._handle_control(payload.encode(), writer)

        written = writer.write.call_args[0][0]
        resp = json.loads(written.decode())
        assert resp['ok'] is True
