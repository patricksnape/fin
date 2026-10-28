"""Locate the data repo. Order: $FIN_DATA, then data_dir in ~/.config/fin/config.toml."""

import os
import tomllib
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

CONFIG_FILE = Path.home() / ".config" / "fin" / "config.toml"


class ConfigError(Exception):
    pass


def data_dir() -> Path:
    env = os.environ.get("FIN_DATA")
    if env:
        path = Path(env).expanduser()
    elif CONFIG_FILE.exists():
        with CONFIG_FILE.open("rb") as f:
            value = tomllib.load(f).get("data_dir")
        if not value:
            raise ConfigError(f"{CONFIG_FILE} has no data_dir")
        path = Path(value).expanduser()
    else:
        raise ConfigError(f"Data repo not configured: set FIN_DATA or data_dir in {CONFIG_FILE}")
    if not path.is_dir():
        raise ConfigError(f"Data directory does not exist: {path}")
    return path


def main_ledger(root: Path | None = None) -> Path:
    return (root or data_dir()) / "ledger" / "main.beancount"


class Settings(BaseModel):
    """Everything the analytics need. Preferences come from config.toml; everything that changes
    what your numbers mean comes from the ledger itself (see ledger_settings)."""

    # from config.toml
    # Payroll deductions and taxes are shown as their own lines, never as "spend"
    non_spend_prefixes: list[str] = ["Expenses:Taxes", "Expenses:Social"]
    # Fees are small and mechanical; they are never offered as "recurring payments"
    no_recurring_prefixes: list[str] = ["Expenses:Fees"]
    # from the ledger
    base_currency: str = "CHF"
    major_threshold: Decimal = Decimal(2000)
    major_accounts: list[str] = []
    employer_commodity: str | None = None
    targets: dict[str, Decimal] = {}


# Keys that used to live in config.toml and now live in the ledger; `fin doctor` flags leftovers.
MOVED_TO_LEDGER = (
    "major_threshold",
    "major_accounts",
    "targets",
    "employer_commodity",
    "base_currency",
)


def stale_config_keys(root: Path) -> list[str]:
    path = root / "config.toml"
    if not path.exists():
        return []
    with path.open("rb") as f:
        return [k for k in tomllib.load(f) if k in MOVED_TO_LEDGER]


def load_settings(root: Path, led) -> Settings:
    """Preferences from config.toml, overlaid with the settings stored in the ledger."""
    from fin import ledger_settings

    path = root / "config.toml"
    prefs = {}
    if path.exists():
        with path.open("rb") as f:
            raw = tomllib.load(f)
        prefs = {k: v for k, v in raw.items() if k not in MOVED_TO_LEDGER}
    ls = ledger_settings.read(led)
    update = {
        "base_currency": ls.base_currency,
        "major_accounts": ls.major_accounts,
        "targets": ls.targets,
    }
    if ls.major_threshold is not None:
        update["major_threshold"] = ls.major_threshold
    if ls.employer_commodity:
        update["employer_commodity"] = ls.employer_commodity
    return Settings(**prefs).model_copy(update=update)
