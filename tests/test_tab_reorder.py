"""Regression tests for B1 — tab drag must not desync the ROM stack.

The tab bar and the QStackedWidget are kept positionally paired (tab i owns
rom_stack.widget(i)). With setMovable(True) the user can drag a tab to a new
position; QTabBar reorders only itself, so without MainWindow.on_tab_moved the
pairing breaks and get_current_document() (and every other index-based lookup)
targets the wrong ROM — edits/saves/FLASH land on the wrong file.
"""

from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QTabBar, QStackedWidget, QWidget

from main import MainWindow

_app = QApplication.instance() or QApplication([])


def _make_harness(names):
    """Build a fake self with a real tab_bar + rom_stack holding named docs.

    on_tab_moved is wired to the tab bar exactly as MainWindow.__init__ wires
    it, and each doc's tab text equals its name so the tab<->stack mapping can
    be asserted per index.
    """
    tab_bar = QTabBar()
    tab_bar.setMovable(True)
    stack = QStackedWidget()
    docs = {}
    for name in names:
        doc = QWidget()
        doc.doc_name = name
        docs[name] = doc
        tab_bar.addTab(name)
        stack.addWidget(doc)

    fake = SimpleNamespace(tab_bar=tab_bar, rom_stack=stack)
    tab_bar.tabMoved.connect(lambda f, t: MainWindow.on_tab_moved(fake, f, t))
    return fake, docs


def _mapping_holds(fake):
    """Every tab index i owns rom_stack.widget(i)."""
    for i in range(fake.tab_bar.count()):
        widget = fake.rom_stack.widget(i)
        if fake.tab_bar.tabText(i) != getattr(widget, "doc_name", None):
            return False
    return True


def test_moving_current_tab_keeps_current_document_identity():
    """Dragging the active tab must keep get_current_document() pointing at it."""
    fake, docs = _make_harness(["A", "B"])
    fake.tab_bar.setCurrentIndex(0)  # A is current
    assert MainWindow.get_current_document(fake) is docs["A"]

    fake.tab_bar.moveTab(0, 1)  # drag A to the second position -> [B, A]

    assert MainWindow.get_current_document(fake) is docs["A"]
    assert _mapping_holds(fake)


def test_moving_other_tab_preserves_current_and_mapping():
    """Dragging a non-active tab must not change which document is current."""
    fake, docs = _make_harness(["A", "B", "C"])
    fake.tab_bar.setCurrentIndex(0)  # A is current
    assert MainWindow.get_current_document(fake) is docs["A"]

    fake.tab_bar.moveTab(1, 2)  # drag B past C -> [A, C, B]

    assert MainWindow.get_current_document(fake) is docs["A"]
    assert _mapping_holds(fake)


def test_move_backwards_reorders_stack():
    """Moving a tab toward the front reorders the stack the same way."""
    fake, docs = _make_harness(["A", "B", "C"])
    fake.tab_bar.setCurrentIndex(2)  # C is current

    fake.tab_bar.moveTab(2, 0)  # drag C to the front -> [C, A, B]

    assert MainWindow.get_current_document(fake) is docs["C"]
    assert _mapping_holds(fake)
    assert [fake.rom_stack.widget(i).doc_name for i in range(3)] == ["C", "A", "B"]
