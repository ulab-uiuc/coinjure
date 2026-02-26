"""Main CLI entry point for Pred Market CLI."""

import click

from pm_cli.cli.agent_commands import backtest, live, paper, strategy
from pm_cli.cli.analytics_commands import analytics
from pm_cli.cli.data_commands import data
from pm_cli.cli.market_commands import market
from pm_cli.cli.monitor import monitor
from pm_cli.cli.news_commands import news
from pm_cli.cli.token import token
from pm_cli.cli.trade_commands import trade


@click.group()
@click.version_option(version='0.0.1')
def cli() -> None:
    """Pred Market CLI - Polymarket trading agent CLI."""
    pass


@cli.command(hidden=True)
@click.option('--duration', default=None, type=float, help='Run duration in seconds.')
@click.option(
    '--exchange', default='rss', type=click.Choice(['polymarket', 'kalshi', 'rss'])
)
@click.option('--initial-capital', default='10000')
@click.option(
    '--strategy-ref',
    default='pm_cli.strategy.test_strategy:TestStrategy',
    show_default=True,
)
@click.pass_context
def run(
    ctx: click.Context,
    duration: float | None,
    exchange: str,
    initial_capital: str,
    strategy_ref: str,
) -> None:
    """Deprecated alias for ``pm-cli paper run``."""
    click.echo('Deprecated: use `pm-cli paper run` instead.')
    paper_run_cmd = paper.commands['run']
    ctx.invoke(
        paper_run_cmd,
        exchange=exchange,
        duration=duration,
        initial_capital=initial_capital,
        strategy_ref=strategy_ref,
        as_json=False,
    )


cli.add_command(monitor)
cli.add_command(token)
cli.add_command(trade)
cli.add_command(strategy)
cli.add_command(backtest)
cli.add_command(paper)
cli.add_command(live)
cli.add_command(news)
cli.add_command(market)
cli.add_command(analytics)
cli.add_command(data)


if __name__ == '__main__':
    cli()
