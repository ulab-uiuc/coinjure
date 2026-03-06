"""Main CLI entry point for Coinjure."""

import click

from coinjure.cli.engine_commands import engine
from coinjure.cli.hub_commands import hub
from coinjure.cli.market_commands import market
from coinjure.cli.memory_commands import memory
from coinjure.cli.research_commands import research
from coinjure.cli.strategy_commands import strategy


@click.group()
@click.version_option(version='0.0.1')
def cli() -> None:
    """Coinjure - trading agent CLI."""
    pass


cli.add_command(market)
cli.add_command(strategy)
cli.add_command(engine)
cli.add_command(hub)
cli.add_command(memory)
cli.add_command(research)


if __name__ == '__main__':
    cli()
