"""Loads the ledger and derived models, reloading when files change."""

from pathlib import Path

from fin import config, ledger
from fin.agent import engine
from fin.analytics import recurring, spend


class Context:
    def __init__(self, root: Path):
        self.root = root
        self._key: tuple | None = None
        self._data: engine.Data | None = None

    def _stamp(self) -> tuple:
        files = [
            *self.root.joinpath("ledger").rglob("*.beancount"),
            self.root / "config.toml",
        ]
        return tuple(sorted((str(f), f.stat().st_mtime_ns) for f in files if f.exists()))

    def load_ledger(self) -> ledger.Ledger:
        """The loaded ledger alone: cheap, and fine to ask even when it is still empty."""
        return ledger.load(config.main_ledger(self.root))

    def data(self) -> engine.Data:
        key = self._stamp()
        if key != self._key or self._data is None:
            led = self.load_ledger()
            if not ledger.has_transactions(led):
                raise ledger.EmptyLedger("the ledger has no transactions yet")
            settings = config.load_settings(self.root, led)
            confirmed, rejected = recurring.load_state(led)
            self._data = engine.Data(
                led, settings, spend.build(led, settings, confirmed, rejected), root=self.root
            )
            self._key = key
        return self._data
