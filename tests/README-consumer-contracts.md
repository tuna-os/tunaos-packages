# Factory consumer contract tests

`tests/consumer-source.json` pins the TunaOS schemas and validators used by
factory tests. CI checks out that exact revision before pytest.

For a local checkout of the recorded revision, run:

```sh
TUNAOS_CONSUMER_ROOT=/path/to/pinned/tunaos just check
```

Alternatively prepare the same checkout at `.test-deps/tunaos`. Tests verify
its full commit identity and perform no network fetches. Install the pinned
Python tools from `requirements-ci.txt`. Update the source pin when changing
the shared contract interface; publish the TunaOS commit before dependent CI.
