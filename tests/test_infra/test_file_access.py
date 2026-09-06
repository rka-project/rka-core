"""Cross-platform path, descriptor, snapshot and traversal policy contracts."""

import os
from pathlib import Path

import pytest

from rka.infra.file_access import FileAccessError, FileAccessPolicy, ScanBudget


@pytest.fixture
def files(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "note.txt").write_bytes(b"allowed bytes")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_bytes(b"PRIVATE-SENTINEL")
    return inbox, outside, FileAccessPolicy([inbox])


def test_authorized_bytes_and_private_snapshot(files):
    inbox, _, policy = files
    assert policy.read_bytes(inbox / "note.txt") == b"allowed bytes"
    with policy.snapshot(inbox / "note.txt") as snapshot:
        assert snapshot.name == "note.txt"
        assert snapshot.read_bytes() == b"allowed bytes"
        assert not snapshot.is_relative_to(inbox)
        (inbox / "note.txt").write_bytes(b"later version")
        assert snapshot.read_bytes() == b"allowed bytes"
    assert not snapshot.exists()


@pytest.mark.parametrize(
    "relative",
    [
        "../outside/note.txt",
        "/outside/note.txt",
        "C:note.txt",
        "C:\\private\\note.txt",
        "\\\\host\\share\\note.txt",
        "\\\\?\\C:\\private.txt",
        "x\x00y",
    ],
)
def test_manifest_relative_paths_do_not_change_authority(files, relative):
    inbox, _, policy = files
    with pytest.raises(FileAccessError):
        policy.read_bytes(policy.relative_file(inbox, relative))


def test_prefix_sibling_is_not_inside_root(files):
    inbox, _, policy = files
    sibling = inbox.parent / "inbox-other"
    sibling.mkdir()
    (sibling / "secret").write_bytes(b"private")
    with pytest.raises(FileAccessError):
        policy.read_bytes(sibling / "secret")


@pytest.mark.parametrize("parent", [False, True])
def test_links_cannot_escape(files, parent):
    inbox, outside, policy = files
    link = inbox / "link"
    try:
        link.symlink_to(outside if parent else outside / "note.txt", target_is_directory=parent)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(FileAccessError):
        policy.read_bytes(link / "note.txt" if parent else link)
    assert list(policy.walk(inbox)) == [inbox / "note.txt"]


def test_loop_and_directory_are_not_read_as_files(files):
    inbox, _, policy = files
    with pytest.raises(FileAccessError):
        policy.read_bytes(inbox)
    try:
        (inbox / "loop").symlink_to(inbox / "loop")
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(FileAccessError):
        policy.read_bytes(inbox / "loop")


@pytest.mark.skipif(os.name == "nt", reason="POSIX special file")
def test_fifo_is_rejected_without_waiting_for_a_writer(files):
    inbox, _, policy = files
    pipe = inbox / "pipe"
    os.mkfifo(pipe)
    with pytest.raises(FileAccessError):
        policy.read_bytes(pipe)


def test_individual_and_aggregate_read_budgets(files):
    inbox, _, policy = files
    with pytest.raises(FileAccessError, match="maximum size"):
        policy.read_bytes(inbox / "note.txt", max_bytes=4)
    budget = ScanBudget(max_bytes=13)
    assert policy.read_bytes(inbox / "note.txt", budget=budget) == b"allowed bytes"
    with pytest.raises(FileAccessError, match="budget"):
        policy.read_bytes(inbox / "note.txt", budget=budget)


def test_enumeration_stops_at_entry_budget_even_if_every_entry_is_ignored(files):
    inbox, _, policy = files
    for index in range(30):
        (inbox / f"ignored-{index}.txt").write_bytes(b"x")
    budget = ScanBudget(max_entries=7)
    assert list(policy.walk(inbox, ignores={"*.txt"}, budget=budget)) == []
    assert budget.entries == 7
    assert "Entry cap" in budget.stopped


def test_file_cap_stops_enumeration_instead_of_counting_the_entire_tree(files):
    inbox, _, policy = files
    for index in range(30):
        (inbox / f"note-{index}.txt").write_bytes(b"x")
    budget = ScanBudget(max_files=3)
    assert len(list(policy.walk(inbox, budget=budget))) == 3
    assert budget.entries == 3
    assert "File cap" in budget.stopped


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor race injection")
def test_parent_swap_is_blocked_before_read(files, monkeypatch):
    inbox, outside, policy = files
    folder = inbox / "docs"
    folder.mkdir()
    (folder / "note.txt").write_bytes(b"allowed")
    original_open = os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "docs" and not swapped:
            swapped = True
            folder.rename(inbox / "docs-original")
            folder.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(FileAccessError):
        policy.read_bytes(folder / "note.txt")
    assert swapped


@pytest.mark.parametrize("raw", ['"/tmp"', "[1]", "not-json"])
def test_host_roots_require_explicit_json_array(monkeypatch, raw):
    monkeypatch.setenv("RKA_HOST_FILE_ROOTS", raw)
    with pytest.raises(ValueError, match="JSON array"):
        FileAccessPolicy.host()


def test_operator_config_cannot_be_replaced_by_request_cwd(files, monkeypatch, tmp_path):
    inbox, _, policy = files
    monkeypatch.chdir(tmp_path)
    assert policy.read_bytes(inbox / "note.txt") == b"allowed bytes"
    with pytest.raises(FileAccessError):
        policy.read_bytes(Path("inbox/note.txt"))


def test_snapshot_concurrency_is_bounded_and_slots_recover(files):
    from contextlib import ExitStack

    inbox, _, policy = files
    with ExitStack() as stack:
        for _ in range(4):
            stack.enter_context(policy.snapshot(inbox / "note.txt"))
        with pytest.raises(FileAccessError) as error:
            policy.read_bytes(inbox / "note.txt")
        assert error.value.code == "file_access_busy"
    assert policy.read_bytes(inbox / "note.txt") == b"allowed bytes"


def test_operator_root_must_not_be_a_symlink(files):
    inbox, outside, _ = files
    link = inbox.parent / "root-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(FileAccessError):
        FileAccessPolicy([link])


def test_scan_slots_are_bounded_and_recover(files):
    from contextlib import ExitStack
    from rka.infra.file_access import scan_slot

    inbox, _, policy = files
    with ExitStack() as stack:
        for _ in range(4):
            stack.enter_context(scan_slot())
        with pytest.raises(FileAccessError) as error:
            list(policy.walk(inbox))
        assert error.value.code == "file_access_busy"
    assert list(policy.walk(inbox)) == [inbox / "note.txt"]


def test_read_deadline_and_depth_limit(files):
    inbox, _, policy = files
    nested = inbox / "nested"
    nested.mkdir()
    (nested / "deep.txt").write_bytes(b"synthetic")
    assert list(policy.walk(inbox, max_depth=0)) == [inbox / "note.txt"]
    budget = ScanBudget()
    budget.deadline = 1
    with pytest.raises(FileAccessError, match="time budget"):
        policy.read_bytes(inbox / "note.txt", budget=budget)


def test_host_scan_skips_oversized_file_without_losing_valid_files(files):
    from rka.services.host_workspace import scan_host_files

    inbox, _, policy = files
    (inbox / "large.txt").write_bytes(b"x" * 100)
    scanned, budget = scan_host_files(policy, inbox, ignores=(), max_bytes=20)
    assert [item["filename"] for item in scanned] == ["note.txt"]
    assert budget.skipped == 1


@pytest.mark.skipif(os.name != "nt", reason="Native Windows junction and handle behavior")
def test_windows_junction_is_denied_and_read_handles_block_replacement(files):
    import subprocess
    from rka.infra.file_access import _windows_open

    inbox, outside, policy = files
    junction = inbox / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(FileAccessError):
        policy.read_bytes(junction / "note.txt")
    assert list(policy.walk(inbox)) == [inbox / "note.txt"]
    with _windows_open(inbox / "note.txt"):
        with pytest.raises(OSError):
            (inbox / "note.txt").write_bytes(b"replacement")
        with pytest.raises(OSError):
            inbox.rename(inbox.parent / "moved")
    assert policy.read_bytes(inbox / "note.txt") == b"allowed bytes"
