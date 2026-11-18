from __future__ import annotations

import os
import tempfile
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from gpu_game_guard.ui import qt  # noqa: E402

if qt.available():
    from gpu_game_guard.ui import theme

    QtCore, QtGui, QtWidgets = qt.QtCore, qt.QtGui, qt.QtWidgets


def key_event(kind, key):
    return QtGui.QKeyEvent(kind, key, QtCore.Qt.NoModifier)


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class ThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()

    def tearDown(self):
        theme.apply_theme(self.app, dark=False)

    def test_app_singleton_and_name(self):
        self.assertIs(qt.app(), self.app)
        self.assertEqual(self.app.applicationName(), "GPU Game Guard")

    def test_colors_for_both_palettes(self):
        light, dark = theme.colors(False), theme.colors(True)
        for palette in (light, dark):
            for field in ("window", "card", "text", "muted", "accent", "accent_text", "border", "ok", "warn", "danger"):
                value = getattr(palette, field)
                self.assertRegex(value, r"^#[0-9a-f]{6}$", field)
        self.assertGreater(QtGui.QColor(light.window).lightness(), QtGui.QColor(dark.window).lightness())
        self.assertNotEqual(light, dark)

    def test_is_dark_follows_the_window_colour(self):
        theme.apply_theme(self.app, dark=True)
        self.assertTrue(theme.is_dark(self.app))
        theme.apply_theme(self.app, dark=False)
        self.assertFalse(theme.is_dark(self.app))

    def test_apply_theme_records_the_palette(self):
        self.assertEqual(theme.apply_theme(self.app, dark=True), theme.DARK)
        self.assertEqual(theme.current_colors(), theme.DARK)
        self.assertTrue(self.app.styleSheet())

    def test_stylesheet_covers_every_component(self):
        for dark in (False, True):
            sheet = theme.stylesheet(theme.colors(dark))
            self.assertTrue(sheet.strip())
            self.assertNotIn("$", sheet)
            for part in ("QPushButton", 'role="primary"', 'role="danger"', "QLineEdit", "QComboBox", "QTabBar",
                         "QTableView", "QHeaderView", "QScrollBar", "QMenu", "QFrame#Card", "QCheckBox", "badge"):
                self.assertIn(part, sheet)
            self.assertIn(theme.colors(dark).accent, sheet)

    def test_stylesheet_assets_are_optional(self):
        self.assertNotIn("url(", theme.stylesheet(theme.LIGHT))
        self.assertIn("url(/x/arrow.png)", theme.stylesheet(theme.LIGHT, {"arrow": "/x/arrow.png"}))

    def test_stylesheet_parses_when_applied(self):
        theme.apply_theme(self.app, dark=False)
        button = theme.button("Go", "primary")
        button.ensurePolished()
        self.assertEqual(button.property("role"), "primary")

    def test_toggle_switch_click_keyboard_and_signal(self):
        switch = theme.ToggleSwitch(accessible_name="Pause Ollama")
        seen = []
        switch.toggled.connect(seen.append)
        self.assertEqual(switch.accessibleName(), "Pause Ollama")
        self.assertFalse(switch.isChecked())
        switch.click()
        self.assertTrue(switch.isChecked())
        press = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, QtCore.QPointF(10, 10), QtCore.Qt.LeftButton,
                                  QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
        release = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonRelease, QtCore.QPointF(10, 10), QtCore.Qt.LeftButton,
                                    QtCore.Qt.NoButton, QtCore.Qt.NoModifier)
        QtWidgets.QApplication.sendEvent(switch, press)
        QtWidgets.QApplication.sendEvent(switch, release)
        self.assertFalse(switch.isChecked())
        for kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):
            QtWidgets.QApplication.sendEvent(switch, key_event(kind, QtCore.Qt.Key_Space))
        self.assertTrue(switch.isChecked())
        self.assertEqual(seen, [True, False, True])
        self.assertEqual(switch.focusPolicy(), QtCore.Qt.StrongFocus)

    def test_toggle_switch_offset_follows_state_and_paints(self):
        switch = theme.ToggleSwitch(True)
        self.assertEqual(switch.offset, 1.0)
        switch.setChecked(False)
        self.assertEqual(switch.offset, 0.0)
        self.assertFalse(switch.grab().isNull())

    def test_card_holds_widgets(self):
        card = theme.Card("Title", "Sub")
        label = QtWidgets.QLabel("x")
        card.addWidget(label)
        self.assertEqual(card.objectName(), "Card")
        self.assertEqual(card.title_label.text(), "Title")
        self.assertIs(card.body.itemAt(0).widget(), label)
        bare = theme.Card()
        self.assertTrue(bare.title_label.isHidden())

    def test_badge_kinds(self):
        badge = theme.Badge("Heavy", "warn")
        self.assertEqual(badge.kind(), "warn")
        badge.setKind("nonsense")
        self.assertEqual(badge.kind(), "muted")
        self.assertEqual(theme.Pill("Paused", "ok").property("pill"), "true")
        for kind in theme.BADGE_KINDS:
            self.assertFalse(theme.Pill("x", kind).grab().isNull())

    def test_section_header(self):
        header = theme.SectionHeader("Targets", "What gets paused")
        self.assertEqual(header.text(), "Targets")
        header.setText("Games")
        self.assertEqual(header.text(), "Games")

    def test_elided_label_shortens_and_keeps_full_text(self):
        label = theme.ElidedLabel("x" * 200)
        label.resize(100, 20)
        self.assertFalse(label.grab().isNull())
        self.assertEqual(label.text(), "x" * 200)
        self.assertEqual(label.toolTip(), "x" * 200)
        self.assertLess(label.minimumSizeHint().width(), 50)

    def test_icons_for_each_state_and_size(self):
        for state in theme.ICON_STATES:
            icon = theme.app_icon(state)
            self.assertFalse(icon.isNull())
            for size in (16, 22, 32, 64):
                pixmap = icon.pixmap(size, size)
                self.assertFalse(pixmap.isNull())
                self.assertEqual(pixmap.width(), size)
                image = pixmap.toImage()
                self.assertGreater(image.pixelColor(size // 2, size // 3).alpha(), 0)
                self.assertEqual(image.pixelColor(0, 0).alpha(), 0)

    def test_icon_states_differ(self):
        images = [theme.app_icon(s).pixmap(32, 32).toImage() for s in theme.ICON_STATES]
        self.assertEqual(len({bytes(i.constBits().asstring(i.byteCount())) for i in images}), len(images))

    def test_render_writes_a_png_without_showing(self):
        widget = theme.demo_widget()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "demo.png")
            self.assertTrue(theme.render(widget, path))
            image = QtGui.QImage(path)
            self.assertGreater(image.width(), 300)
            self.assertGreater(image.height(), 300)
        self.assertFalse(widget.isVisible())

    def test_demo_renders_in_both_schemes(self):
        for dark in (False, True):
            theme.apply_theme(self.app, dark=dark)
            self.assertFalse(theme.demo_widget().grab().isNull())


class WithoutQtTests(unittest.TestCase):
    def test_available_is_a_cached_bool(self):
        self.assertIsInstance(qt.available(), bool)
        self.assertEqual(qt.available(), qt.available())

    def test_unknown_attribute_raises(self):
        with self.assertRaises(AttributeError):
            qt.QtNothing  # noqa: B018


if __name__ == "__main__":
    unittest.main()
