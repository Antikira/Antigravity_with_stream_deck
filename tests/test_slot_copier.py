"""Unit tests for profile slot copier components."""

import json
from pathlib import Path

import pytest

pytest.importorskip("src.profile_slot_copier")

from src.profile_slot_copier.backup import BackupManager
from src.profile_slot_copier.cli import parse_slot_spec
from src.profile_slot_copier.copier import (
    SlotCopier,
    expand_action_env_vars,
    regenerate_action_ids,
)
from src.profile_slot_copier.scanner import (
    extract_image_references,
    load_profile,
    scan_all_profiles,
)


def create_mock_profile(base_dir: Path, profile_name: str, uuid_str: str) -> Path:
    """Helper to create a mock .sdProfile tree for testing."""
    prof_dir = base_dir / f"{uuid_str}.sdProfile"
    prof_dir.mkdir(parents=True)

    page_uuid = "11111111-2222-3333-4444-555555555555"
    page_dir = prof_dir / "Profiles" / page_uuid
    images_dir = page_dir / "Images"
    images_dir.mkdir(parents=True)

    # Dummy image file
    sample_img = images_dir / "SAMPLE_ICON.png"
    sample_img.write_text("dummy image data", encoding="utf-8")

    # Page manifest
    page_manifest = {
        "Name": "Page 1",
        "Controllers": [
            {
                "Type": "Keypad",
                "Actions": {
                    "0,0": {
                        "ActionID": "orig-action-id-00",
                        "Name": "Test Action 1",
                        "UUID": "com.test.action",
                        "Plugin": {"Name": "TestPlugin", "UUID": "com.test.plugin"},
                        "States": [{"Title": "Button 1", "Image": "Images/SAMPLE_ICON.png"}],
                    }
                },
            }
        ],
    }
    with open(page_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(page_manifest, f)

    # Profile manifest
    prof_manifest = {
        "Name": profile_name,
        "Device": {"Model": "20GBA9901"},
        "Pages": {"Current": page_uuid, "Default": page_uuid, "Pages": [page_uuid]},
    }
    with open(prof_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(prof_manifest, f)

    return prof_dir


def test_extract_image_references():
    data = {
        "ActionID": "123",
        "States": [{"Image": "Images/test1.png"}, {"Image": "Images/test2.png"}],
        "Nested": {"States": [{"Image": "Images/test1.png"}]},  # Duplicate
    }
    refs = extract_image_references(data)
    assert refs == ["Images/test1.png", "Images/test2.png"]


def test_regenerate_action_ids():
    data = {
        "ActionID": "old-id-1",
        "Actions": [{"Actions": [{"ActionID": "old-id-2"}]}],
    }
    regenerated = regenerate_action_ids(data)
    assert regenerated["ActionID"] != "old-id-1"
    assert regenerated["Actions"][0]["Actions"][0]["ActionID"] != "old-id-2"


def test_expand_action_env_vars(monkeypatch):
    monkeypatch.setenv("TEST_BASE", "C:/Users/testuser")
    data = {
        "Settings": {
            "path": "%TEST_BASE%/app.exe",
            "nested": ["%TEST_BASE%/sub/file.txt", "normal_string"],
            "number": 42,
        }
    }
    expanded = expand_action_env_vars(data)
    assert expanded["Settings"]["path"] == "C:/Users/testuser/app.exe"
    assert expanded["Settings"]["nested"][0] == "C:/Users/testuser/sub/file.txt"
    assert expanded["Settings"]["nested"][1] == "normal_string"
    assert expanded["Settings"]["number"] == 42


def test_parse_slot_spec():
    p, pg, c = parse_slot_spec("VS code:(0,1)")
    assert p == "VS code"
    assert pg is None
    assert c == "0,1"

    p, pg, c = parse_slot_spec("Default[page-uuid]:1,2")
    assert p == "Default"
    assert pg == "page-uuid"
    assert c == "1,2"

    with pytest.raises(ValueError):
        parse_slot_spec("InvalidSpec")


def test_scanner_loading(tmp_path: Path):
    create_mock_profile(tmp_path, "Dev Profile", "AAAA-BBBB-CCCC")
    profiles = scan_all_profiles(tmp_path)

    assert len(profiles) == 1
    prof = profiles[0]
    assert prof.name == "Dev Profile"
    assert prof.uuid == "AAAA-BBBB-CCCC"
    assert len(prof.pages) == 1

    page = prof.pages[0]
    assert "0,0" in page.actions
    action = page.actions["0,0"]
    assert action.name == "Test Action 1"
    assert action.title == "Button 1"
    assert action.image_rel_paths == ["Images/SAMPLE_ICON.png"]


def test_backup_and_restore(tmp_path: Path):
    prof_dir = create_mock_profile(tmp_path / "profiles", "Backup Test", "TEST-PROFILE")
    backup_root = tmp_path / "backups"
    bm = BackupManager(backup_root=backup_root)

    # Create backup
    record = bm.create_backup(prof_dir, profile_name="Backup Test", note="Initial state")
    assert record.backup_dir.is_dir()

    # Verify backup listing
    records = bm.list_backups("TEST-PROFILE")
    assert len(records) == 1
    assert records[0].profile_name == "Backup Test"

    # Modify original file
    manifest_file = prof_dir / "manifest.json"
    manifest_file.write_text('{"Name": "Modified"}', encoding="utf-8")
    assert "Modified" in manifest_file.read_text(encoding="utf-8")

    # Restore from backup
    bm.restore_backup(record)
    restored_text = manifest_file.read_text(encoding="utf-8")
    assert "Backup Test" in restored_text


def test_slot_copier_between_profiles(tmp_path: Path):
    profiles_dir = tmp_path / "profiles"
    prof_src_dir = create_mock_profile(profiles_dir, "Source Profile", "SRC-UUID")
    prof_dst_dir = create_mock_profile(profiles_dir, "Target Profile", "DST-UUID")

    backup_dir = tmp_path / "backups"
    bm = BackupManager(backup_dir)
    copier = SlotCopier(backup_manager=bm)

    prof_src = load_profile(prof_src_dir)
    prof_dst = load_profile(prof_dst_dir)
    assert prof_src is not None
    assert prof_dst is not None

    src_page = prof_src.pages[0]
    dst_page = prof_dst.pages[0]

    # Target slot 1,1 is currently empty
    assert "1,1" not in dst_page.actions

    # Copy 0,0 to 1,1
    result = copier.copy_slot(
        source_page=src_page,
        source_coord="0,0",
        target_profile=prof_dst,
        target_page=dst_page,
        target_coord="1,1",
        auto_backup=True,
    )

    assert result.success is True
    assert "1,1" in dst_page.actions
    copied_action = dst_page.actions["1,1"]
    assert copied_action.name == "Test Action 1"
    assert copied_action.action_id != src_page.actions["0,0"].action_id

    # Check image was copied to target Images/
    target_img = dst_page.images_dir / "SAMPLE_ICON.png"
    assert target_img.is_file()
    assert target_img.read_text(encoding="utf-8") == "dummy image data"


def test_clear_slot(tmp_path: Path):
    profiles_dir = tmp_path / "profiles"
    prof_dir = create_mock_profile(profiles_dir, "Clear Test", "CLEAR-UUID")
    prof = load_profile(prof_dir)
    assert prof is not None

    page = prof.pages[0]
    assert "0,0" in page.actions

    copier = SlotCopier(BackupManager(tmp_path / "backups"))
    clear_res = copier.clear_slot(prof, page, "0,0", auto_backup=True)

    assert clear_res.success is True
    assert "0,0" not in page.actions


def test_template_export_and_injection(tmp_path: Path):
    profiles_dir = tmp_path / "profiles"
    prof_dir = create_mock_profile(profiles_dir, "Template Test", "TPL-UUID")
    prof = load_profile(prof_dir)
    assert prof is not None

    page = prof.pages[0]
    copier = SlotCopier(BackupManager(tmp_path / "backups"))

    template_file = tmp_path / "templates" / "test_action.json"
    copier.export_slot_template(
        slot_action=page.actions["0,0"],
        source_images_dir=page.images_dir,
        output_json_path=template_file,
        template_name="Exported Action",
    )

    assert template_file.is_file()
    bundle_img = tmp_path / "templates" / "test_action_images" / "SAMPLE_ICON.png"
    assert bundle_img.is_file()

    # Inject template into slot 2,2
    inject_res = copier.load_and_inject_template(
        template_json_path=template_file,
        target_profile=prof,
        target_page=page,
        target_coord="2,2",
        auto_backup=False,
    )

    assert inject_res.success is True
    assert "2,2" in page.actions
    assert page.actions["2,2"].name == "Test Action 1"


def test_sync_profile_page_with_preserve(tmp_path: Path):
    profiles_dir = tmp_path / "profiles"
    src_dir = create_mock_profile(profiles_dir, "Source Prof", "SRC-UUID")
    dst_dir = create_mock_profile(profiles_dir, "Target Prof", "DST-UUID")

    src_prof = load_profile(src_dir)
    dst_prof = load_profile(dst_dir)
    assert src_prof and dst_prof

    src_page = src_prof.pages[0]
    dst_page = dst_prof.pages[0]

    copier = SlotCopier(BackupManager(tmp_path / "backups"))

    # Put a specific action on target slot 0,2
    copier.inject_action(
        action_data={"Name": "Keep Me", "UUID": "com.keep.me"},
        source_images_dir=None,
        target_profile=dst_prof,
        target_page=dst_page,
        target_coord="0,2",
        auto_backup=False,
    )
    # Put another action on src slot 0,2 and 1,0
    copier.inject_action(
        action_data={"Name": "Overwrite Candidate", "UUID": "com.src.action"},
        source_images_dir=None,
        target_profile=src_prof,
        target_page=src_page,
        target_coord="0,2",
        auto_backup=False,
    )
    copier.inject_action(
        action_data={"Name": "New Action", "UUID": "com.src.new"},
        source_images_dir=None,
        target_profile=src_prof,
        target_page=src_page,
        target_coord="1,0",
        auto_backup=False,
    )

    # Sync with preserve_coords=["0,2"]
    results = copier.sync_profile_page(
        source_profile=src_prof,
        target_profile=dst_prof,
        preserve_coords=["0,2"],
        auto_backup=False,
    )

    assert len(results) > 0
    # 0,2 on target should still be "Keep Me"
    assert dst_page.actions["0,2"].name == "Keep Me"
    # 1,0 on target should now exist and be "New Action"
    assert dst_page.actions["1,0"].name == "New Action"
