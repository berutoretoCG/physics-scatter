"""Physics Scatter: Substance 3D Designer plugin.

Nothing Designer-specific is imported at module level so that the engine
subpackage can run in a plain Python process (python -m physics_scatter.engine).
"""

_panel = None


def initializeSDPlugin():
    global _panel
    from .panel import install
    _panel = install()


def uninitializeSDPlugin():
    global _panel
    from .panel import uninstall
    uninstall(_panel)
    _panel = None
