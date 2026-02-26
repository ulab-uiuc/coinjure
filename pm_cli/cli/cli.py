"""Main CLI entry point for Pred Market CLI."""

import click

from pm_cli.cli.agent_commands import backtest, live, paper, strategy
from pm_cli.cli.data_commands import data
from pm_cli.cli.market_commands import market
from pm_cli.cli.monitor import monitor
from pm_cli.cli.news_commands import news
from pm_cli.cli.research_commands import research
from pm_cli.cli.token import token
from pm_cli.cli.trade_commands import trade


@click.group()
@click.version_option(version='0.0.1')
def cli() -> None:
    """Pred Market CLI - Polymarket trading agent CLI."""
    pass


cli.add_command(monitor)
cli.add_command(token)
cli.add_command(trade)
cli.add_command(strategy)
cli.add_command(backtest)
cli.add_command(paper)
cli.add_command(live)
cli.add_command(news)
cli.add_command(market)
cli.add_command(data)
cli.add_command(research)


if __name__ == '__main__':
    cli()
