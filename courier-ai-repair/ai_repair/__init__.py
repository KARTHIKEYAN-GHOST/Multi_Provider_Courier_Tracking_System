"""AI repair service: fixes a courier adapter that stopped working, or writes one for a new provider.

It is a separate program. It holds no database, RabbitMQ or Redis access and no provider
credentials. It gets its work from the Engine's internal API, lets an AI model propose code,
checks that code itself (``guard``), runs its tests in a sandbox without network
(``sandbox``) and only then commits and pushes it.
"""
