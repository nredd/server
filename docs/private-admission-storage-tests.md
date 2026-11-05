# Private admission storage tests

The offline controller tests exercise the startup reset and its flush before browser setup,
core target set and clear, and the retained player configuration after nonpermanent unregister.
A deliberately parked save demonstrates that a core-save return is not a durability guarantee;
an explicit flush establishes the persisted state. A cleared default is omitted from raw storage,
so the admission preflight intentionally fails closed until a startup reset writes the raw value.

These tests do not establish full AirPlay reload behavior, real network or receiver traffic,
cache database behavior, acoustic output, or stale-race integration. External browser and player
services are isolated; the tests do not contact devices.
