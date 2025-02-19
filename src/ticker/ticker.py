from abc import ABC, abstractmethod

class Ticker(ABC):
    @property
    @abstractmethod
    def symbol(self) -> str:
        """The symbol of the ticker, must be unique across all markets"""
        pass 