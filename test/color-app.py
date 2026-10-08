#!/usr/bin/env python3
"""Test client: a real (GTK3) app window filled with magenta.

    color-app.py [--fullscreen] [--menu]

Runs as a Wayland or X11 (Xwayland) client depending on GDK_BACKEND.
--menu adds a button at the top left whose dropdown menu (a real popup
surface) is cyan, so a test can click it open and capture it.
"""

import sys

import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk  # noqa: E402

css = Gtk.CssProvider()
css.load_from_data(b"""
    window, .magenta { background-color: #ff00ff; }
    menu, menuitem { background-color: #00ffff; color: #00ffff;
                     min-height: 120px; min-width: 200px; }
""")
Gtk.StyleContext.add_provider_for_screen(
    Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_USER)

win = Gtk.Window(title='zt-color')
win.set_default_size(600, 400)
win.set_decorated(False)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
box.get_style_context().add_class('magenta')
win.add(box)
if '--menu' in sys.argv:
    menu = Gtk.Menu()
    menu.append(Gtk.MenuItem(label='Eichhörnchen'))
    menu.show_all()
    button = Gtk.MenuButton(label='open')
    button.set_use_popover(False)
    button.set_popup(menu)
    button.set_halign(Gtk.Align.START)
    box.pack_start(button, False, False, 0)
if '--fullscreen' in sys.argv:
    win.fullscreen()
win.connect('destroy', Gtk.main_quit)
win.show_all()
Gtk.main()
