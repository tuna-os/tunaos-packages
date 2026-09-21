"""Shared package-factory tooling.

Command-line entry points live beside importable modules in this package.  A
script launched directly has this directory on ``sys.path``; tests and other
Python callers can import the same modules through ``scripts``.
"""
