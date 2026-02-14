"""Main CLI entry point for SWM Agent."""

import click

from swm_agent.cli.monitor import monitor


@click.group()
@click.version_option(version='0.0.1')
def cli() -> None:
    """SWM Agent - Polymarket trading agent CLI."""
    pass


cli.add_command(monitor)


if __name__ == '__main__':
    cli()
