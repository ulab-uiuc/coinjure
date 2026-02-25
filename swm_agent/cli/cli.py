"""Main CLI entry point for SWM Agent."""

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


@cli.command(hidden=True)
@click.option('--duration', default=None, type=float, help='Run duration in seconds.')
@click.option(
    '--exchange', default='rss', type=click.Choice(['polymarket', 'kalshi', 'rss'])
)
@click.option('--initial-capital', default='10000')
@click.option(
    '--strategy-ref',
    default='swm_agent.strategy.test_strategy:TestStrategy',
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
    """Deprecated alias for ``swm-agent paper run``."""
    click.echo('Deprecated: use `swm-agent paper run` instead.')
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


if __name__ == '__main__':
    cli()
