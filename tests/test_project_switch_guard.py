"""Regression tests for B4 — one project open at a time.

The ProjectManager is a singleton (one current_project). Opening a second
project used to rebind it while the first project's tab stayed open, so that
tab would then commit/revert into the second project's history. The guard
closes the current project first (prompting interactively) or aborts.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication, QMessageBox

from src.ui.project_mixin import ProjectMixin

_app = QApplication.instance() or QApplication([])


def _fake_with_open_project(current_path="C:/A", name="A"):
    pm = MagicMock()
    pm.is_project_open.return_value = True
    pm.current_project.project_path = current_path
    pm.current_project.name = name
    return SimpleNamespace(
        project_manager=pm,
        _find_open_tab=MagicMock(),
        close_tab=MagicMock(),
        statusBar=lambda: MagicMock(),
    )


def test_proceeds_when_no_project_open():
    pm = MagicMock()
    pm.is_project_open.return_value = False
    fake = SimpleNamespace(project_manager=pm)
    assert ProjectMixin._ensure_single_project(fake, "C:/B", prompt=True) is True


def test_proceeds_for_same_project():
    fake = _fake_with_open_project(current_path="C:/A")
    # Same path (different separators) is the same project — allowed.
    assert ProjectMixin._ensure_single_project(fake, "C:/A", prompt=True) is True
    fake.close_tab.assert_not_called()


def test_restore_skips_second_project_without_prompt():
    fake = _fake_with_open_project(current_path="C:/A")
    with patch.object(QMessageBox, "question") as q:
        result = ProjectMixin._ensure_single_project(fake, "C:/B", prompt=False)
    assert result is False
    q.assert_not_called()  # no modal during session restore
    fake.close_tab.assert_not_called()
    fake.project_manager.close_project.assert_not_called()


def test_interactive_switch_declined_aborts():
    fake = _fake_with_open_project(current_path="C:/A")
    with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
        result = ProjectMixin._ensure_single_project(fake, "C:/B", prompt=True)
    assert result is False
    fake.close_tab.assert_not_called()
    fake.project_manager.close_project.assert_not_called()


def test_interactive_switch_accepted_closes_current():
    fake = _fake_with_open_project(current_path="C:/A")
    # Tab found at index 3, then gone after close_tab (close succeeded).
    fake._find_open_tab.side_effect = [3, -1]
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        result = ProjectMixin._ensure_single_project(fake, "C:/B", prompt=True)
    assert result is True
    fake.close_tab.assert_called_once_with(3)
    fake.project_manager.close_project.assert_called_once()


def test_switch_aborts_if_close_cancelled_by_unsaved_prompt():
    fake = _fake_with_open_project(current_path="C:/A")
    # Tab still present after close_tab -> user cancelled the unsaved prompt.
    fake._find_open_tab.side_effect = [3, 3]
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        result = ProjectMixin._ensure_single_project(fake, "C:/B", prompt=True)
    assert result is False
    fake.close_tab.assert_called_once_with(3)
    fake.project_manager.close_project.assert_not_called()


# --- orphaned binding cleanup -------------------------------------------------
# A failure AFTER open_project() binds the manager (missing definition XML,
# load_definition error) used to leave it bound with no tab; the B4 guard then
# silently blocked every subsequent project open against an invisible project.


def _open_path_fake(find_open_tab, bound_path="C:/A"):
    pm = MagicMock()
    pm.is_project_open.return_value = False
    project = MagicMock()
    project.project_path = bound_path
    project.original_rom.rom_id = "LF9VEB"
    # open_project binds current_project as the real manager does.
    pm.current_project = None

    def bind(path):
        pm.current_project = project
        return project

    pm.open_project.side_effect = bind
    return SimpleNamespace(
        project_manager=pm,
        rom_detector=MagicMock(),
        _find_open_tab=find_open_tab,
        _ensure_single_project=lambda p, prompt: True,
    )


def test_definition_not_found_unbinds_the_tabless_project():
    fake = _open_path_fake(find_open_tab=MagicMock(return_value=-1))
    fake.rom_detector.find_definition_by_id.return_value = None
    with patch("src.ui.project_mixin.QMessageBox"):
        ProjectMixin.open_project_path(fake, "C:/A")
    fake.project_manager.close_project.assert_called_once()


def test_load_failure_after_bind_unbinds_the_tabless_project():
    from src.core.exceptions import RomEditorError

    fake = _open_path_fake(find_open_tab=MagicMock(return_value=-1))
    fake.rom_detector.find_definition_by_id.return_value = "C:/defs/lf9veb.xml"
    with (
        patch(
            "src.ui.project_mixin.load_definition", side_effect=RomEditorError("boom")
        ),
        patch("src.ui.project_mixin.handle_rom_operation_error"),
    ):
        ProjectMixin.open_project_path(fake, "C:/A")
    fake.project_manager.close_project.assert_called_once()


def test_bound_project_with_tab_is_left_alone():
    # First call (already-open check) -> no tab; finally-check -> tab exists.
    fake = _open_path_fake(find_open_tab=MagicMock(side_effect=[-1, 5]))
    fake.rom_detector.find_definition_by_id.return_value = None
    with patch("src.ui.project_mixin.QMessageBox"):
        ProjectMixin.open_project_path(fake, "C:/A")
    fake.project_manager.close_project.assert_not_called()
