import json
import logging
from decimal import Decimal
from typing import Literal, TypedDict

import requests

from swm_agent.events.events import Event, NewsEvent, OrderBookEvent
from swm_agent.ticker.ticker import Ticker
from swm_agent.trader.trader import Trader
from swm_agent.trader.types import TradeSide

from .strategy import Strategy

url = 'https://api.siliconflow.cn/v1/chat/completions'


class LLMAnalysisResult(TypedDict):
    action: Literal['buy', 'sell', 'hold']
    confidence: float
    reasoning: str | None


class SimpleStrategy(Strategy):
    def __init__(
        self, trade_size: Decimal = Decimal('1.0'), confidence_threshold: float = 0.3
    ):
        """
        Args:
            confidence_threshold: Minimum confidence level from LLM required to execute trades
        """
        self.trade_size = trade_size
        self.confidence_threshold = confidence_threshold
        self.logger = logging.getLogger(__name__)

    async def process_event(self, event: Event, trader: Trader) -> None:
        """Process incoming events and make trading decisions."""
        if isinstance(event, OrderBookEvent):
            self.logger.info(f'OrderBookEvent: {event}')
        elif isinstance(event, NewsEvent):
            self.logger.info(f'NewsEvent: {event.title}')

            ticker = Ticker(event.ticker)

            # Call LLM to analyze the news
            analysis = await self._analyze_news_with_llm(event)

            self.logger.info(f'LLM Analysis: {analysis}')

            # Check confidence level
            if analysis['confidence'] < self.confidence_threshold:
                self.logger.info(
                    f'Discarding event due to low confidence: {analysis["confidence"]}'
                )
                return

            # Execute trade based on LLM suggestion
            await self._execute_trade(analysis, ticker, trader)

    async def _analyze_news_with_llm(self, event: NewsEvent) -> LLMAnalysisResult:
        """Call the LLM API to analyze the news event."""
        try:
            # Create a prompt that asks for trading action and confidence
            prompt = f"""
            Based on the following news, determine if an investor should BUY, SELL, or HOLD.
            Also provide a confidence level between 0 and 1.

            Headline: {event.title}
            Content: {event.news}
            Source: {event.source}

            Respond in JSON format only, with the following structure:
            {{
                "action": "buy" or "sell" or "hold",
                "confidence": [number between 0 and 1],
            }}
            """

            payload = {
                'model': 'deepseek-ai/DeepSeek-R1-Distill-Llama-8B',
                'messages': [{'role': 'user', 'content': prompt}],
                'stream': False,
                'max_tokens': 512,
                'temperature': 0.3,
                'response_format': {'type': 'json_object'},
            }

            headers = {
                'Authorization': 'Bearer sk-htbiwgijayenuxogigyisgtruuzevfbgaqnhlaodeszaztlt',
                'Content-Type': 'application/json',
            }

            response = requests.request('POST', url, json=payload, headers=headers)
            response.raise_for_status()

            result = response.json()

            # Parse the JSON response from the LLM
            analysis = json.loads(result)

            # Ensure the response is properly formatted
            if not all(k in analysis for k in ['action', 'confidence']):
                raise ValueError('LLM response missing required fields')

            return {
                'action': analysis['action'].lower(),
                'confidence': float(analysis['confidence']),
            }

        except (
            requests.RequestException,
            json.JSONDecodeError,
            ValueError,
            KeyError,
        ) as e:
            self.logger.error(f'Error analyzing news with LLM: {e}')
            # Return a safe default if analysis fails
            return {
                'action': 'hold',
                'confidence': 0.0,
                'reasoning': f'Analysis error: {str(e)}',
            }

    async def _execute_trade(
        self, analysis: LLMAnalysisResult, ticker: Ticker, trader: Trader
    ) -> None:
        """Execute a trade based on the LLM analysis."""
        try:
            if analysis['action'] == 'buy':
                self.logger.info(
                    f'Executing market order BUY for {ticker.symbol} with confidence {analysis["confidence"]}'
                )
                # Get the current market price
                price = await trader.market_data.get_best_ask(ticker)

                # Execute the buy order
                result = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=ticker,
                    limit_price=price,
                    quantity=self.trade_size,
                )
                self.logger.info(f'Buy order result: {result}')

            elif analysis['action'] == 'sell':
                self.logger.info(
                    f'Executing market order SELL for {ticker.symbol} with confidence {analysis["confidence"]}'
                )
                # Get the current market price
                price = await trader.market_data.get_best_bid(ticker)

                # Execute the sell order
                result = await trader.place_order(
                    side=TradeSide.SELL,
                    ticker=ticker,
                    limit_price=price,
                    quantity=self.trade_size,
                )
                self.logger.info(f'Sell order result: {result}')

            else:
                self.logger.info(f'No trade action taken (hold) for {ticker.symbol}')

        except Exception as e:
            self.logger.error(f'Error executing trade: {e}')
