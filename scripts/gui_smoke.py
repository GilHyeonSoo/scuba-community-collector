"""Construct every desktop tab and dialogs without making network requests."""

import sys
import tempfile
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collector.config import ConfigStore, new_source
from collector.secrets import SecretStore
from collector.storage import Store
from collector.ui import App, SourceDialog

with tempfile.TemporaryDirectory() as temporary:
    home = Path(temporary)
    root = tk.Tk()
    app = App(root, ConfigStore(home), Store(home), SecretStore(home))
    root.update()
    app.toggle_demo()
    root.update()
    assert app.store.stats()["total"] == 6
    first = app.data_tree.get_children()[0]
    app.data_tree.selection_set(first)
    app.show_record()
    assert "ID:" in app.detail.get("1.0", "end")
    dialog = SourceDialog(root, new_source(), lambda _: None)
    root.update()
    dialog.destroy()
    for action in (app.credentials, app.settings):
        action()
        root.update()
        for widget in root.winfo_children():
            if isinstance(widget, tk.Toplevel):
                widget.destroy()
    for tab in app.notebook.tabs():
        app.notebook.select(tab)
        root.update()
    app.toggle_demo()
    assert app.store.stats()["total"] == 0
    root.destroy()
print("GUI smoke passed: tabs, details, source dialog, credentials, settings, isolated demo.")
