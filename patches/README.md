# Recording-path patcher

`apply_recording_paths.py` intentionally uses exact upstream code-block replacement instead of `git apply`.

Why:

- the custom layer touches only two known upstream Rust files;
- every replacement must match exactly once or the build stops;
- upstream refactors therefore fail closed instead of applying a fuzzy patch to the wrong place;
- the capability probe runs before this patcher and pauses publication when upstream appears to add equivalent native support.

The patched checkout is always compiled/tested and both Docker architectures are built before `latest` can be published.
