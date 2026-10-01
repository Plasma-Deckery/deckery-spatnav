#!/usr/bin/env python3
"""Focused test (deckery#28): can an AT-SPI EventListener + GLib timeout be
made to dispatch on a dedicated worker thread's own GMainContext, instead of
the global-default (main-thread) context, using explicit Source.attach()
instead of the GLib.idle_add()/timeout_add() convenience wrappers?

Prints which OS thread each callback actually runs on. Main thread runs a
GLib.MainLoop on the global-default context (simulating GTK's own loop).
"""

import threading
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

main_thread_id = threading.get_ident()
worker_thread_id = None


def on_timeout():
    tid = threading.get_ident()
    where = "WORKER" if tid == worker_thread_id else ("MAIN" if tid == main_thread_id else "OTHER")
    print(f"[timeout] fired on thread {tid} ({where})")
    return True


def on_atspi_event(event):
    tid = threading.get_ident()
    where = "WORKER" if tid == worker_thread_id else ("MAIN" if tid == main_thread_id else "OTHER")
    print(f"[atspi-event] {event.type} fired on thread {tid} ({where})")


def worker_main():
    global worker_thread_id
    worker_thread_id = threading.get_ident()
    print(f"[worker] thread id = {worker_thread_id}")

    context = GLib.MainContext.new()
    context.push_thread_default()

    # Explicit Source + attach(context) instead of GLib.timeout_add()
    src = GLib.timeout_source_new(500)
    src.set_callback(on_timeout)
    src.attach(context)

    listener = Atspi.EventListener.new(on_atspi_event)
    listener.register("window:activate")
    listener.register("object:state-changed:showing")
    print("[worker] registered AT-SPI listener + explicit-context timeout, entering loop")

    loop = GLib.MainLoop(context)
    loop.run()


def main():
    print(f"[main] thread id = {main_thread_id}")
    t = threading.Thread(target=worker_main, daemon=True)
    t.start()
    time.sleep(15)
    print("[main] done waiting, exiting")


if __name__ == "__main__":
    main()
