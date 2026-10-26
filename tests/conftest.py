"""Collection rules for the suite."""

# numpy is the optional [splat] extra (the dev extra installs it). Without it the tests that build or decode
# splat clouds cannot run; the numpy-free paths of validation.oversize are tested in test_validation_oversize.py.
try:
    import numpy  # noqa: F401
except ImportError:
    collect_ignore = ["splat_fixtures.py", "test_splat_arrays.py", "test_splat_spz.py", "test_splat_ply.py",
                      "test_splat_chunk.py", "test_splat_inspect.py", "test_splat_tools.py", "test_splat_http.py"]
