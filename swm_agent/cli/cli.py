"""Main CLI entry point for SWM Agent."""

import asyncio
import logging
from decimal import Decimal

import click

from swm_agent.cli.agent_commands import backtest, live, paper, strategy
from swm_agent.cli.monitor import monitor
from swm_agent.cli.token import token
from swm_agent.cli.trade_commands import trade


@click.group()
@click.version_option(version='0.0.1')
def cli() -> None:
    """SWM Agent - Polymarket trading agent CLI."""
    pass


@cli.command()
@click.option('--config', 'config_path', default=None, help='Path to JSON config file.')
@click.option('--paper', is_flag=True, default=False, help='Force paper trading mode.')
@click.option('--duration', default=None, type=float, help='Run duration in seconds.')
def run(config_path: str | None, paper: bool, duration: float | None) -> None:
    """Run the trading agent.

    \b
    Examples:
      swm-agent run --config config.json --paper --duration 3600
      swm-agent run --paper --duration 300
    """
    from swm_agent.alerts.alerter import CompositeAlerter, LogAlerter
    from swm_agent.alerts.telegram_alerter import TelegramAlerter
    from swm_agent.config.config import Config
    from swm_agent.data.live.live_data_source import LiveRSSNewsDataSource
    from swm_agent.live.live_trader import run_live_paper_trading
    from swm_agent.storage.state_store import StateStore
    from swm_agent.strategy.test_strategy import TestStrategy

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )

    cfg = Config.from_file(config_path) if config_path else Config.defaults()
    if not paper:
        click.echo(
            'Only paper mode is currently wired in CLI run; defaulting to paper mode.'
        )

    # Build state store
    state_store = StateStore(cfg.storage.data_dir)

    # Build alerters
    alerters = [LogAlerter(cfg.storage.data_dir)]
    if (
        cfg.alerts.enabled
        and cfg.alerts.telegram.bot_token
        and cfg.alerts.telegram.chat_id
    ):
        alerters.append(
            TelegramAlerter(
                bot_token=cfg.alerts.telegram.bot_token,
                chat_id=cfg.alerts.telegram.chat_id,
            )
        )
    alerter = CompositeAlerter(alerters)

    # Build data source
    data_source = LiveRSSNewsDataSource(
        polling_interval=60.0,
        max_articles_per_poll=5,
    )

    # Build strategy (simple placeholder — users can extend)
    strategy = TestStrategy()
    drawdown_alert_pct = cfg.alerts.thresholds.drawdown_pct_alert
    drawdown_threshold = (
        None if drawdown_alert_pct is None else Decimal(str(drawdown_alert_pct))
    )

    click.echo(f'Starting paper trading (data_dir={cfg.storage.data_dir})')

    asyncio.run(
        run_live_paper_trading(
            data_source=data_source,
            strategy=strategy,
            initial_capital=cfg.engine.initial_capital,
            duration=duration,
            state_store=state_store,
            alerter=alerter,
            continuous=cfg.engine.continuous,
            drawdown_alert_pct=drawdown_threshold,
        )
    )


cli.add_command(monitor)
cli.add_command(token)
cli.add_command(trade)
cli.add_command(strategy)
cli.add_command(backtest)
cli.add_command(paper)
cli.add_command(live)


if __name__ == '__main__':
    cli()
