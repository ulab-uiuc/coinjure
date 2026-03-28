from decimal import Decimal

from coinjure.data.order_book import Level, OrderBook


class TestLevel:
    def test_level_creation(self):
        """Test creating a Level."""
        level = Level(price=Decimal('0.50'), size=Decimal('1000'))
        assert level.price == Decimal('0.50')
        assert level.size == Decimal('1000')

    def test_level_str(self):
        """Test Level string representation."""
        level = Level(price=Decimal('0.50'), size=Decimal('1000'))
        assert str(level) == '0.50@1000'

    def test_level_repr(self):
        """Test Level repr."""
        level = Level(price=Decimal('0.50'), size=Decimal('1000'))
        assert "Level(price=Decimal('0.50')" in repr(level)


class TestOrderBook:
    def test_empty_order_book(self):
        """Test empty order book."""
        ob = OrderBook()
        assert ob.asks == []
        assert ob.bids == []
        assert ob.best_ask is None
        assert ob.best_bid is None

    def test_update_order_book(self):
        """Test updating order book."""
        ob = OrderBook()

        asks = [
            Level(price=Decimal('0.55'), size=Decimal('500')),
            Level(price=Decimal('0.56'), size=Decimal('300')),
        ]
        bids = [
            Level(price=Decimal('0.50'), size=Decimal('1000')),
            Level(price=Decimal('0.49'), size=Decimal('800')),
        ]

        ob.update(asks=asks, bids=bids)

        assert len(ob.asks) == 2
        assert len(ob.bids) == 2

    def test_best_ask(self):
        """Test getting best ask."""
        ob = OrderBook()
        asks = [
            Level(price=Decimal('0.55'), size=Decimal('500')),
            Level(price=Decimal('0.56'), size=Decimal('300')),
        ]
        ob.update(asks=asks, bids=[])

        best = ob.best_ask
        assert best is not None
        assert best.price == Decimal('0.55')
        assert best.size == Decimal('500')

    def test_best_bid(self):
        """Test getting best bid."""
        ob = OrderBook()
        bids = [
            Level(price=Decimal('0.50'), size=Decimal('1000')),
            Level(price=Decimal('0.49'), size=Decimal('800')),
        ]
        ob.update(asks=[], bids=bids)

        best = ob.best_bid
        assert best is not None
        assert best.price == Decimal('0.50')
        assert best.size == Decimal('1000')

    def test_get_asks_with_depth(self):
        """Test getting asks with depth limit."""
        ob = OrderBook()
        asks = [
            Level(price=Decimal('0.55'), size=Decimal('500')),
            Level(price=Decimal('0.56'), size=Decimal('300')),
            Level(price=Decimal('0.57'), size=Decimal('200')),
        ]
        ob.update(asks=asks, bids=[])

        top_2 = ob.get_asks(depth=2)
        assert len(top_2) == 2
        assert top_2[0].price == Decimal('0.55')
        assert top_2[1].price == Decimal('0.56')

    def test_get_bids_with_depth(self):
        """Test getting bids with depth limit."""
        ob = OrderBook()
        bids = [
            Level(price=Decimal('0.50'), size=Decimal('1000')),
            Level(price=Decimal('0.49'), size=Decimal('800')),
            Level(price=Decimal('0.48'), size=Decimal('600')),
        ]
        ob.update(asks=[], bids=bids)

        top_2 = ob.get_bids(depth=2)
        assert len(top_2) == 2
        assert top_2[0].price == Decimal('0.50')
        assert top_2[1].price == Decimal('0.49')

    def test_get_asks_no_depth(self):
        """Test getting all asks without depth limit."""
        ob = OrderBook()
        asks = [
            Level(price=Decimal('0.55'), size=Decimal('500')),
            Level(price=Decimal('0.56'), size=Decimal('300')),
        ]
        ob.update(asks=asks, bids=[])

        all_asks = ob.get_asks()
        assert len(all_asks) == 2

    def test_order_book_str(self):
        """Test order book string representation."""
        ob = OrderBook()
        asks = [Level(price=Decimal('0.55'), size=Decimal('500'))]
        bids = [Level(price=Decimal('0.50'), size=Decimal('1000'))]
        ob.update(asks=asks, bids=bids)

        s = str(ob)
        assert 'best_bid' in s
        assert 'best_ask' in s

    def test_order_book_repr(self):
        """Test order book repr."""
        ob = OrderBook()
        asks = [Level(price=Decimal('0.55'), size=Decimal('500'))]
        bids = [Level(price=Decimal('0.50'), size=Decimal('1000'))]
        ob.update(asks=asks, bids=bids)

        r = repr(ob)
        assert 'OrderBook' in r
        assert 'asks=' in r
        assert 'bids=' in r

    def test_spread(self):
        """Test spread property."""
        ob = OrderBook()
        asks = [Level(price=Decimal('0.55'), size=Decimal('500'))]
        bids = [Level(price=Decimal('0.50'), size=Decimal('1000'))]
        ob.update(asks=asks, bids=bids)
        assert ob.spread == Decimal('0.05')

    def test_spread_empty(self):
        """Test spread returns None when a side is empty."""
        ob = OrderBook()
        assert ob.spread is None
        ob.update(asks=[Level(price=Decimal('0.55'), size=Decimal('500'))], bids=[])
        assert ob.spread is None
        ob.update(asks=[], bids=[Level(price=Decimal('0.50'), size=Decimal('1000'))])
        assert ob.spread is None

    def test_validate_valid_book(self):
        ob = OrderBook()
        asks = [Level(price=Decimal('0.55'), size=Decimal('500')), Level(price=Decimal('0.56'), size=Decimal('300'))]
        bids = [Level(price=Decimal('0.50'), size=Decimal('1000')), Level(price=Decimal('0.49'), size=Decimal('800'))]
        ob.update(asks=asks, bids=bids)
        assert ob.validate() is True

    def test_validate_empty_book(self):
        ob = OrderBook()
        assert ob.validate() is True

    def test_validate_unsorted_bids(self):
        ob = OrderBook()
        bids = [Level(price=Decimal('0.49'), size=Decimal('800')), Level(price=Decimal('0.50'), size=Decimal('1000'))]
        ob.update(asks=[], bids=bids)
        assert ob.validate() is False

    def test_validate_unsorted_asks(self):
        ob = OrderBook()
        asks = [Level(price=Decimal('0.56'), size=Decimal('300')), Level(price=Decimal('0.55'), size=Decimal('500'))]
        ob.update(asks=asks, bids=[])
        assert ob.validate() is False

    def test_validate_negative_price(self):
        ob = OrderBook()
        bids = [Level(price=Decimal('-0.01'), size=Decimal('100'))]
        ob.update(asks=[], bids=bids)
        assert ob.validate() is False

    def test_validate_negative_size(self):
        ob = OrderBook()
        asks = [Level(price=Decimal('0.55'), size=Decimal('-10'))]
        ob.update(asks=asks, bids=[])
        assert ob.validate() is False

    def test_cumulative_size(self):
        ob = OrderBook()
        asks = [Level(price=Decimal('0.55'), size=Decimal('500')), Level(price=Decimal('0.56'), size=Decimal('300')), Level(price=Decimal('0.57'), size=Decimal('200'))]
        bids = [Level(price=Decimal('0.50'), size=Decimal('1000')), Level(price=Decimal('0.49'), size=Decimal('800')), Level(price=Decimal('0.48'), size=Decimal('600'))]
        ob.update(asks=asks, bids=bids)
        bid_size, ask_size = ob.cumulative_size(depth_levels=2)
        assert bid_size == Decimal('1800')
        assert ask_size == Decimal('800')

    def test_cumulative_size_empty_book(self):
        ob = OrderBook()
        bid_size, ask_size = ob.cumulative_size()
        assert bid_size == Decimal('0')
        assert ask_size == Decimal('0')


class TestOrderBookHelpers:
    """Tests for best_bid_price, best_ask_price, mid_price, depth."""

    def _make_book(self) -> OrderBook:
        ob = OrderBook()
        asks = [Level(price=Decimal('0.55'), size=Decimal('500')), Level(price=Decimal('0.56'), size=Decimal('300')), Level(price=Decimal('0.57'), size=Decimal('200'))]
        bids = [Level(price=Decimal('0.50'), size=Decimal('1000')), Level(price=Decimal('0.49'), size=Decimal('800')), Level(price=Decimal('0.48'), size=Decimal('600'))]
        ob.update(asks=asks, bids=bids)
        return ob

    def test_best_bid_price(self):
        assert self._make_book().best_bid_price == Decimal('0.50')

    def test_best_ask_price(self):
        assert self._make_book().best_ask_price == Decimal('0.55')

    def test_best_bid_price_empty(self):
        assert OrderBook().best_bid_price is None

    def test_best_ask_price_empty(self):
        assert OrderBook().best_ask_price is None

    def test_mid_price(self):
        expected = (Decimal('0.50') + Decimal('0.55')) / 2
        assert self._make_book().mid_price == expected

    def test_mid_price_empty(self):
        assert OrderBook().mid_price is None

    def test_depth_bid(self):
        assert self._make_book().depth('bid', levels=2) == Decimal('1800')

    def test_depth_ask(self):
        assert self._make_book().depth('ask', levels=2) == Decimal('800')

    def test_depth_default_levels(self):
        ob = self._make_book()
        assert ob.depth('bid') == Decimal('2400')
        assert ob.depth('ask') == Decimal('1000')

    def test_depth_empty(self):
        assert OrderBook().depth('bid') == Decimal('0')
        assert OrderBook().depth('ask') == Decimal('0')
