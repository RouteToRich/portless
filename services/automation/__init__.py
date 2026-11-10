"""Automated test runs against the ``portless_automation`` database.

The API (``router.py``) lives in the monolith and only starts, reads and clears
runs. Scenarios execute in a separate worker process (``worker.py``) whose
settings point at ``portless_automation``, so the running application and its
own database are never involved.
"""
