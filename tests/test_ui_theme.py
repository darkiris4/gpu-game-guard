from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from gpu_game_guard.ui import qt  # noqa: E402

if qt.available():
    from gpu_game_guard.ui import theme

    QtCore, QtGui, QtWidgets = qt.QtCore, qt.QtGui, qt.QtWidgets


def luminance(colour):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    c = QtGui.QColor(colour)
    return 0.2126 * channel(c.red()) + 0.7152 * channel(c.green()) + 0.0722 * channel(c.blue())


def contrast(first, second):
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def blend(foreground, background, alpha):
    a, b = QtGui.QColor(foreground), QtGui.QColor(background)
    return "#{:02x}{:02x}{:02x}".format(*[round(x * alpha + y * (1 - alpha)) for x, y in
                                          ((a.red(), b.red()), (a.green(), b.green()), (a.blue(), b.blue()))])


HOSTILE = '<img src="file:///etc/passwd"><a href="file:///etc/passwd">x</a><b>B</b>'


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
        self.assertIn('url("/x/arrow.png")', theme.stylesheet(theme.LIGHT, {"arrow": "/x/arrow.png"}))

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
        self.assertEqual(label.toolTip(), "<qt>" + "x" * 200 + "</qt>")
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


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class SafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()

    def test_safe_text_strips_controls_and_bidi(self):
        raw = "a\x00b\x07c\u202ed\u2066e\u2069f\u200eg\u200fh\u061ci\x7fj\x85k"
        self.assertEqual(theme.safe_text(raw), "abcdefghijk")
        self.assertEqual(theme.safe_text("a\nb\tc\r\nd"), "a b c  d")
        self.assertEqual(theme.safe_text("a\nb\tc\r\nd", keep_newlines=True), "a\nb c\nd")
        self.assertEqual(theme.safe_text(None), "")
        self.assertEqual(theme.safe_text(12), "12")
        self.assertEqual(theme.safe_text("plain <b>text</b> \u00e9"), "plain <b>text</b> \u00e9")

    def test_labels_are_plain_text_by_default(self):
        label = theme.styled_label(HOSTILE, "muted")
        self.assertEqual(label.textFormat(), QtCore.Qt.PlainText)
        self.assertEqual(label.text(), HOSTILE)
        callout = theme.Callout(HOSTILE, "warn")
        self.assertEqual(callout.label.textFormat(), QtCore.Qt.PlainText)
        for widget in (theme.Badge(HOSTILE), theme.Pill(HOSTILE), theme.SectionHeader(HOSTILE, HOSTILE),
                       theme.Card(HOSTILE, HOSTILE), theme.ElidedLabel(HOSTILE)):
            for child in [widget, *widget.findChildren(QtWidgets.QLabel)]:
                if isinstance(child, QtWidgets.QLabel):
                    self.assertEqual(child.textFormat(), QtCore.Qt.PlainText, type(widget).__name__)

    def test_rich_is_an_explicit_opt_in(self):
        self.assertEqual(theme.styled_label("<a href='x'>x</a>", rich=True).textFormat(), QtCore.Qt.RichText)
        callout = theme.Callout("<b>x</b>", rich=True)
        self.assertEqual(callout.label.textFormat(), QtCore.Qt.RichText)
        self.assertEqual(theme.styled_label("x", "", None, False, False).wordWrap(), False)

    def test_plain_tooltip_is_never_rich_text(self):
        widget = QtWidgets.QLabel()
        theme.plain_tooltip(widget, HOSTILE)
        tip = widget.toolTip()
        self.assertTrue(tip.startswith("<qt>") and tip.endswith("</qt>"))
        self.assertNotIn("<img", tip)
        self.assertNotIn("<a ", tip)
        self.assertIn("&lt;img src=", tip)
        doc = QtGui.QTextDocument()
        doc.setHtml(tip)
        self.assertEqual(doc.toPlainText().strip(), HOSTILE)
        theme.plain_tooltip(widget, "")
        self.assertEqual(widget.toolTip(), "")

    def test_elided_label_cleans_and_escapes_its_tooltip(self):
        label = theme.ElidedLabel("<b>" + "x" * 300 + "\u202e\n")
        label.resize(80, 20)
        label.grab()
        self.assertNotIn("\u202e", label.text())
        self.assertNotIn("<b>", label.toolTip())
        self.assertIn("&lt;b&gt;", label.toolTip())


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class AssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch("gpu_game_guard.ui.theme.log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def test_images_are_written_to_a_private_directory(self):
        assets = theme._write_assets(theme.LIGHT, self.root)
        self.assertEqual(set(assets), {"arrow", "check"})
        for path in assets.values():
            self.assertTrue(Path(path).is_file())
            self.assertTrue(path.startswith(str(self.root)))
            self.assertFalse(QtGui.QImage(path).isNull())
            self.assertEqual(os.stat(path).st_mode & 0o077, 0)
        self.assertEqual(os.stat(Path(assets["arrow"]).parent).st_mode & 0o077, 0)

    def test_no_runtime_directory_means_no_images(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(theme._write_assets(theme.LIGHT), {})

    def test_a_planted_symlink_is_not_followed(self):
        victim = self.root / "victim.txt"
        victim.write_text("keep")
        base = self.root / "base"
        base.mkdir()
        target = self.root / "elsewhere"
        target.mkdir()
        (base / "gpu-game-guard").symlink_to(target)
        self.assertEqual(theme._write_assets(theme.LIGHT, base), {})
        self.assertEqual(list(target.iterdir()), [])
        directory = theme._write_assets(theme.LIGHT, self.root)["arrow"]
        planted = Path(directory)
        planted.unlink()
        planted.symlink_to(victim)
        self.assertEqual(theme._write_assets(theme.LIGHT, self.root), {})
        self.assertEqual(victim.read_text(), "keep")

    def test_a_failed_save_is_reported_and_the_theme_still_applies(self):
        with mock.patch.object(QtGui.QPixmap, "save", return_value=False):
            self.assertEqual(theme._write_assets(theme.LIGHT, self.root), {})
        self.assertTrue(self.log.called)
        with mock.patch("gpu_game_guard.ui.theme._write_assets", return_value={}):
            self.assertEqual(theme.apply_theme(self.app, dark=False), theme.LIGHT)
        self.assertNotIn("url(", self.app.styleSheet())

    def test_apply_theme_uses_the_given_root_and_skips_headless_platforms(self):
        theme.apply_theme(self.app, dark=False)
        self.assertNotIn("url(", self.app.styleSheet())  # offscreen: nothing is written anywhere
        theme.apply_theme(self.app, dark=False, asset_root=self.root)
        self.assertIn(str(self.root), self.app.styleSheet())
        theme.apply_theme(self.app, dark=False)


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class ScalingAndContrastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()

    def tearDown(self):
        theme.apply_theme(self.app, dark=False)

    def test_high_dpi_attributes_are_enabled(self):
        self.assertTrue(QtCore.QCoreApplication.testAttribute(QtCore.Qt.AA_EnableHighDpiScaling))
        self.assertTrue(QtCore.QCoreApplication.testAttribute(QtCore.Qt.AA_UseHighDpiPixmaps))

    def test_headline_stays_larger_than_body_text_when_the_font_grows(self):
        original = self.app.font()
        try:
            for points in (10, 13, 16):
                font = QtGui.QFont(original)
                font.setPointSizeF(points)
                self.app.setFont(font)
                for dark in (False, True):
                    theme.apply_theme(self.app, dark=dark)
                    head, title, body = (theme.styled_label("Aa", r) for r in ("headline", "title", ""))
                    small = theme.styled_label("Aa", "small")
                    for widget in (head, title, body, small):
                        widget.ensurePolished()
                    self.assertGreater(head.font().pointSizeF(), title.font().pointSizeF())
                    self.assertGreater(title.font().pointSizeF(), points)
                    self.assertGreaterEqual(head.font().pointSizeF(), 1.4 * points)
                    self.assertLess(small.font().pointSizeF(), points)
        finally:
            self.app.setFont(original)

    def test_fixed_sizes_follow_the_font(self):
        original = self.app.font()
        try:
            sizes = []
            for points in (10, 16):
                font = QtGui.QFont(original)
                font.setPointSizeF(points)
                self.app.setFont(font)
                switch = theme.ToggleSwitch()
                sizes.append(switch.sizeHint())
                theme.apply_theme(self.app, dark=False, asset_root=None)
            self.assertGreaterEqual(sizes[1].width(), int(1.5 * sizes[0].width()))
            self.assertGreaterEqual(sizes[1].height(), int(1.5 * sizes[0].height()))
            small = theme.stylesheet(theme.LIGHT, {"check": "/c.png"}, 1.0)
            large = theme.stylesheet(theme.LIGHT, {"check": "/c.png"}, 1.6)
            self.assertIn("width: 18px", small)
            self.assertIn("width: 29px", large)
        finally:
            self.app.setFont(original)

    def test_badge_text_has_enough_contrast_on_its_tint(self):
        for dark in (False, True):
            p = theme.colors(dark)
            for kind in ("ok", "warn", "danger"):
                colour = getattr(p, kind)
                for surface in (p.window, p.card):
                    self.assertGreaterEqual(contrast(colour, blend(colour, surface, 0.16)), 4.5, (dark, kind))
            for surface in (p.window, p.card, p.hover):
                self.assertGreaterEqual(contrast(p.muted, surface), 4.5)

    def test_switch_off_track_has_enough_contrast(self):
        for dark in (False, True):
            p = theme.colors(dark)
            track = theme.mix(p.border, p.muted, 0.7)
            for surface in (p.window, p.card):
                self.assertGreaterEqual(contrast(track, surface), 3.0, (dark, surface))
        switch = theme.ToggleSwitch(False)
        theme.apply_theme(self.app, dark=False)
        image = switch.grab().toImage()
        self.assertGreaterEqual(contrast(image.pixelColor(image.width() // 2, 3).name(), theme.LIGHT.window), 3.0)

    def test_focus_rings_exist_for_every_kind_of_control(self):
        sheet = theme.stylesheet(theme.LIGHT, {"arrow": "/a.png", "check": "/c.png"})
        for selector in ("QPushButton:focus", 'QPushButton[role="primary"]:focus', 'QPushButton[role="danger"]:focus',
                         "QLineEdit:focus", "QComboBox:focus", "QSpinBox:focus", "QCheckBox::indicator:focus",
                         "QRadioButton::indicator:focus", "QTabBar::tab:focus", "QTableView:focus"):
            self.assertIn(selector, sheet)
        switch = theme.ToggleSwitch()
        switch.resize(switch.sizeHint())
        plain = switch.grab().toImage()
        switch.setFocus()
        window = QtWidgets.QWidget()
        switch.setParent(window)
        window.show()
        switch.setFocus(QtCore.Qt.TabFocusReason)
        self.app.processEvents()
        ringed = switch.grab().toImage()
        window.close()
        self.assertNotEqual(plain, ringed)


class WithoutQtTests(unittest.TestCase):
    def test_available_is_a_cached_bool(self):
        self.assertIsInstance(qt.available(), bool)
        self.assertEqual(qt.available(), qt.available())

    def test_unknown_attribute_raises(self):
        with self.assertRaises(AttributeError):
            qt.QtNothing  # noqa: B018


if __name__ == "__main__":
    unittest.main()
