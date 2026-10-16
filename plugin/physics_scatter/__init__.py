"""Physics Scatter: Substance 3D Designer plugin.

Nothing Designer-specific is imported at module level so that the engine
subpackage can run in a plain Python process (python -m physics_scatter.engine).
"""

_installed = None


def initializeSDPlugin():
    global _installed
    from .simulator import install
    _installed = install()


def uninitializeSDPlugin():
    global _installed
    from .simulator import uninstall
    uninstall(_installed)
    _installed = None
