"""Navigation prototype: a client of the v2 backend (BACKEND_V2_DESIGN.md §10).

Holds the backend's target list locally, decides selection and hysteresis
itself, moves the pointer. Which modes exist and how they feel is open —
every mode here is a named, switchable preset (presets.toml), never wired in."""
