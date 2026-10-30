from fin.importers import viac


def test_the_recorder_finds_its_captures_and_books_nothing_yet(tmp_path):
    folder = tmp_path / "sources" / "viac"
    folder.mkdir(parents=True)
    (folder / "2026-10-30_main_capture.json").write_text("{}")
    (folder / "notes.txt").write_text("")
    assert [p.name for p in viac.source_files(tmp_path)] == ["2026-10-30_main_capture.json"]
    assert viac.parse_files(viac.source_files(tmp_path)) == []
    assert viac.IN_SYNC is False
