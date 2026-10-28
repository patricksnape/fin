import pytest

from fin import scaffold


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / "data"
    scaffold.init_data(root)
    monkeypatch.setenv("FIN_DATA", str(root))
    return root


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Tests never read or write the real ~/.config/fin/settings.json."""
    monkeypatch.setenv("FIN_SETTINGS", str(tmp_path / "settings.json"))
