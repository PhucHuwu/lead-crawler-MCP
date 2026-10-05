"""Test doubles, kept out of ``src`` so nothing importable ships a fake.

The rule this package exists to enforce: a mock is a *test* concern. Production
code reaches a browser only through :mod:`src.browser`, so the doubles here
implement the protocols from :mod:`src.browser.base` and nothing in ``src``
knows they exist.
"""
