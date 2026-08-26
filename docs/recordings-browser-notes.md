# Recordings browser regression

The upstream `/v1/videos` and `/static/{path}` flows assume media files live in the process working directory. `biliup-custom` stores recordings under `/recordings/<streamer>/<logical-date>/...`, so manual upload selection and the History page must use the recordings tree as their single source of truth.
