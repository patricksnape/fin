"""Importers: deterministic parsers that turn a source's raw files into ledger transactions.

A source module provides SOURCE, fetch(root, start, end, log), source_files(root) and
parse_files(files). Register it here to make it available to `fin fetch`, `fin import`, `fin sync`.
"""

import importlib
from types import ModuleType

SOURCES = {
    "swisscard": "fin.importers.swisscard",
    "ubs": "fin.importers.ubs",
    "wise": "fin.importers.wise",
    "ibkr": "fin.importers.ibkr",
    "schwab": "fin.importers.schwab",
    "ubs-pdf": "fin.importers.ubs_pdf",
    "ubs-mortgage": "fin.importers.ubs_mortgage",
}


def get(name: str) -> ModuleType:
    if name not in SOURCES:
        raise KeyError(f"unknown source {name!r}; known: {', '.join(SOURCES)}")
    return importlib.import_module(SOURCES[name])
