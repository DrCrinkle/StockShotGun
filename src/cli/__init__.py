"""CLI command handlers extracted from main.py.

main.py remains the dispatcher; each handler module here owns one command path.
Dependency flow is strictly one-directional: main -> cli.* -> cli.common, never
the reverse, so there are no circular imports.
"""
