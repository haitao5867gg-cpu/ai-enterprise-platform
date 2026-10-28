"""Flow apps: apps whose structure is a configuration people edit, not code.

A flow app's configuration (``schema_version``, ``env`` and ``graph``, see
docs/api/flow-apps.md) is versioned like any app's. ``check`` checks it before it
is saved, ``graph`` compiles it into a LangGraph graph, ``nodes`` holds the node
types, ``builtin`` the shipped app and the templates, ``store`` the apps.
"""
