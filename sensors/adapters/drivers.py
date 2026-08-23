"""Which live drivers this build knows how to construct.

`factory.py` resolves a registry entry's `driver` name against a table and knows nothing about
what is in it. This is the table's contents, and it is the only file that has to change when a
device is added — one import and one registration, with the resolution logic untouched.

Kept apart from the factory rather than folded into it because the two answer different
questions. The factory answers *how is a placement turned into an adapter*; this answers *what
can this build talk to*, which is a deployment fact and the thing someone greps for when a
registry entry names a driver that does not exist.

Registration is explicit rather than an import side effect. A driver that appeared in the table
because something happened to import its package would make the set of available hardware depend
on import order, and the failure — a sensor that builds on one entry point and not another —
would be miserable to diagnose.
"""

from __future__ import annotations

from sensors.adapters.factory import known_drivers, register_driver


def register_builtin_drivers() -> tuple[str, ...]:
    """Make every driver shipped with Aetos buildable. Idempotent.

    Called by whatever wires a deployment together — `backend/main.py` and the conformance
    CLI — rather than at import time. Returns the full driver table afterwards, so a caller
    that wants to report what it can talk to does not have to ask separately.
    """
    from sensors.adapters.ld2451.adapter import build_ld2451_adapter  # noqa: PLC0415

    # Idempotent because more than one entry point may wire the same process — the conformance
    # CLI inside a test that has already built an application, for instance — and a second
    # registration is not a mistake worth failing on. `register_driver` still refuses a *second
    # implementation* under the same name, which is the case that matters.
    if "ld2451_uart" not in known_drivers():
        register_driver("ld2451_uart", build_ld2451_adapter)

    return known_drivers()
