"""PortLess service modules.

Each subpackage is an independently deployable FastAPI service. Services
communicate only over REST (via the ``client.py`` in each module), so any of
them can be lifted into its own microservice without code changes.
"""
