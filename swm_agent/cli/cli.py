"""Main CLI entry point for SWM Agent."""

import click

from swm_agent.cli.monitor import monitor
from swm_agent.cli.token import token
from swm_agent.cli.trade_commands import trade


@click.group()
@click.version_option(version='0.0.1')
def cli() -> None:
    """SWM Agent - Polymarket trading agent CLI."""
    pass


cli.add_command(monitor)
cli.add_command(token)
cli.add_command(trade)


if __name__ == '__main__':
    cli()
