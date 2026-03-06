"""Main CLI entry point for Coinjure."""

import click

from coinjure.cli.agent_commands import backtest, live, paper, strategy
from coinjure.cli.arb_commands import arb, market_match_cmd
from coinjure.cli.hub_commands import hub
from coinjure.cli.market_commands import market
from coinjure.cli.monitor import monitor
from coinjure.cli.news_commands import news
from coinjure.cli.portfolio_commands import portfolio
from coinjure.cli.research_commands import research
from coinjure.cli.trade_commands import trade


@click.group()
@click.version_option(version='0.0.1')
def cli() -> None:
    """Coinjure - trading agent CLI."""
    pass


cli.add_command(monitor)
cli.add_command(hub)
cli.add_command(trade)
cli.add_command(strategy)
cli.add_command(backtest)
cli.add_command(paper)
cli.add_command(live)
cli.add_command(market)
cli.add_command(news)
cli.add_command(research)
cli.add_command(portfolio)
cli.add_command(arb)

# Add `market match` as a sub-command of the existing `market` group.
market.add_command(market_match_cmd)


if __name__ == '__main__':
    cli()
