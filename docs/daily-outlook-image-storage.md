# Daily outlook image storage

The first successful chart-image request renders the saved report snapshot and
atomically stores its PNG (base64), download filename, support/resistance legend,
and channel metadata together in `DATA_DIR/media/daily-outlook/run_<id>.json`.
Subsequent visits read that same artifact without running the renderer or geometry
calculation. Existing reports are initialized lazily on their first visit after
deployment; no market fetch or AI inference is triggered.

Compose sets DATA_DIR to /app/data and mounts ./data there, so artifacts survive
container replacement. Back up this directory alongside the database. Renderer
upgrades do not change already stored reports. There is no automatic eviction.
Deleting an artifact allows it to be recreated on its next request.

A per-report OS file lock serializes concurrent first requests across processes.
A temporary file and atomic rename prevent partial artifacts; failed writes can
be retried. The authenticated endpoint still validates report existence/completion
and keeps private no-store HTTP headers; loading the stored file is not regeneration.
