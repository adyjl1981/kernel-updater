"""Shared theme regression checks; GUI checks run when a display is available."""
import os
import subprocess
import tkinter as tk
from tkinter import font, ttk
import unittest
from unittest import mock

import ubuntu_theme as theme


class DetectionTests(unittest.TestCase):
    def test_all_accents_and_old_yaru_variants(self):
        for name, color in theme.ACCENTS.items():
            with self.subTest(name=name):
                self.assertEqual(theme.detect_accent({'accent-color': name}), color)
                self.assertEqual(theme.detect_accent({'gtk-theme': f'Yaru-{name}-dark'}), color)
        self.assertEqual(theme.detect_accent({'gtk-theme': 'Yaru-dark'}), '#E95420')

    def test_explicit_accent_wins(self):
        self.assertEqual(theme.detect_accent({'accent-color': 'blue', 'gtk-theme': 'Yaru-red'}), '#0073E5')

    def test_unknown_and_malformed_fallback(self):
        for prefs in ({}, {'accent-color': 'unknown'}, {'accent-color': []},
                      {'gtk-theme': 'Other-blue-dark'}, {'gtk-theme': None}):
            self.assertEqual(theme.detect_accent(prefs), '#E95420')
        self.assertEqual(theme.detect_accent({'accent-color': 'unknown', 'gtk-theme': 'Yaru-sage'}), '#657B69')

    @mock.patch.dict(os.environ, {'XDG_CURRENT_DESKTOP': 'ubuntu:GNOME'})
    @mock.patch('ubuntu_theme.shutil.which', return_value='/usr/bin/gsettings')
    def test_settings_and_absent_keys(self, which):
        output = "org.gnome.desktop.interface accent-color 'pink'\norg.gnome.desktop.interface color-scheme 'prefer-dark'\norg.gnome.desktop.interface gtk-theme 'Yaru'\norg.gnome.desktop.interface font-name invalid\n"
        with mock.patch('ubuntu_theme.subprocess.run', return_value=subprocess.CompletedProcess([], 0, output, '')) as run:
            prefs = theme.desktop_preferences()
            self.assertEqual(prefs['accent-color'], 'pink')
            self.assertEqual(prefs['color-scheme'], 'prefer-dark')
            self.assertNotIn('font-name', prefs)
            self.assertEqual(run.call_args.args[0], ['gsettings', 'list-recursively', 'org.gnome.desktop.interface'])
        for result in (subprocess.CompletedProcess([], 1, '', 'no schema'), subprocess.CompletedProcess([], 0, '', '')):
            with mock.patch('ubuntu_theme.subprocess.run', return_value=result):
                self.assertEqual(theme.desktop_preferences(), {})
        for error in (OSError(), subprocess.TimeoutExpired('gsettings', 1)):
            with mock.patch('ubuntu_theme.subprocess.run', side_effect=error):
                self.assertEqual(theme.desktop_preferences(), {})

    def test_other_desktops_and_missing_command(self):
        with mock.patch.dict(os.environ, {'XDG_CURRENT_DESKTOP': 'KDE'}), mock.patch('ubuntu_theme.subprocess.run') as run:
            self.assertEqual(theme.desktop_preferences(), {})
            run.assert_not_called()
        with mock.patch.dict(os.environ, {'XDG_CURRENT_DESKTOP': 'GNOME'}), mock.patch('ubuntu_theme.shutil.which', return_value=None):
            self.assertEqual(theme.desktop_preferences(), {})

    def test_contrast_and_appearance(self):
        for mode in ('prefer-light', 'prefer-dark'):
            for name in theme.ACCENTS:
                colors = theme.palette({'color-scheme': mode, 'accent-color': name})
                for key in ('accent', 'accent_hover', 'accent_pressed'):
                    self.assertGreaterEqual(theme.contrast(colors[key], colors['accent_text']), 4.5)
                for key in ('window', 'surface'):
                    self.assertGreaterEqual(theme.contrast(colors[key], colors['focus']), 3)
                    self.assertGreaterEqual(theme.contrast(colors[key], colors['text']), 4.5)
                self.assertGreaterEqual(theme.contrast(colors['inactive_selection'], colors['text']), 4.5)
        self.assertEqual(theme.palette({'gtk-theme': 'Yaru-dark'})['window'], theme.LIGHT['window'])


class GuiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_shared_styles_and_existing_dialog_refresh(self):
        theme.apply_theme(self.root, {})
        dialog = tk.Toplevel(self.root)
        text = tk.Text(dialog, **theme.text_options(dialog))
        notebook = ttk.Notebook(dialog)
        style = ttk.Style(self.root)
        for mode in ('prefer-light', 'prefer-dark'):
            for name in theme.ACCENTS:
                colors = theme.apply_theme(self.root, {'color-scheme': mode, 'accent-color': name})
                self.assertEqual(dialog.cget('background'), colors['window'])
                self.assertEqual(text.cget('selectbackground'), colors['accent'])
                for widget, option, state in (
                    ('TNotebook.Tab', 'background', ('selected',)),
                    ('Accent.TButton', 'background', ('active',)),
                    ('TCheckbutton', 'indicatorbackground', ('selected',)),
                    ('TRadiobutton', 'indicatorbackground', ('selected',)),
                    ('Treeview', 'background', ('selected', 'focus'))):
                    expected = colors['accent_hover'] if state == ('active',) else colors['accent']
                    self.assertEqual(style.lookup(widget, option, state), expected)
                self.assertEqual(style.lookup('Horizontal.TProgressbar', 'background'), colors['accent'])
                self.root.update_idletasks()

    def test_runtime_refresh_and_stable_font_size(self):
        with mock.patch.object(theme, 'desktop_preferences', return_value={'text-scaling-factor': 1.5}), mock.patch.object(self.root, 'after') as after:
            theme.apply_theme(self.root)
            start = after.call_args.args[1]
            before = font.nametofont('TkDefaultFont', root=self.root).actual('size')
            with mock.patch.object(theme.threading, 'Thread') as thread:
                start()
                thread.call_args.kwargs['target']()
            collect = after.call_args.args[1]
            collect()
            self.assertEqual(font.nametofont('TkDefaultFont', root=self.root).actual('size'), before)
            with mock.patch.object(theme, 'desktop_preferences', return_value={'accent-color': 'purple'}), mock.patch.object(theme.threading, 'Thread') as thread:
                start()
                thread.call_args.kwargs['target']()
                collect()
            self.assertEqual(self.root._ubuntu_colors['accent'], theme.ACCENTS['purple'])
