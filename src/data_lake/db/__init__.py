"""Declarative models for the shareable half of the schema.

:mod:`data_lake.db.base` holds ``Base`` and the two portable column types;
:mod:`data_lake.db.models` holds the tables themselves.

Deliberately empty of re-exports and of any engine or session: this package never opens a
database connection of its own. The consumer owns the engine, runs the migrations, and hands
sessions in through :func:`data_lake.configure`. A consumer that also has private tables
declares them against this ``Base`` so a single ``Base.metadata`` still covers everything
Alembic needs to see.
"""
