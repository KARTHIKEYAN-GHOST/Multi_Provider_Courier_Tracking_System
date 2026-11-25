"""The human interface: a small website where a user answers challenges.

It is a thin client of the Engine's public API. It has no database, no RabbitMQ access and
no access to Redis or provider credentials: everything it shows and does goes through
``/v1/interventions`` with the signed-in user's own API key.
"""
